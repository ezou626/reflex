from __future__ import annotations

from pathlib import Path
from typing import Any

from config.schema import SysctlKind, TunerCatalogEntry

from tuners.base import AppliedAction, BaseTuner, TunerAction
from tuners.sysctl_util import read_sysctl, sysctl_name_to_path, write_sysctl


class GenericSysctlTuner(BaseTuner):
    """Runtime sysctl effector built from catalog entry."""

    def __init__(self, entry: TunerCatalogEntry) -> None:
        if entry.scope != "runtime_sysctl":
            raise ValueError("GenericSysctlTuner requires runtime_sysctl scope")
        self._entry = entry
        self.tuner_id = entry.id
        self.sysctl_name = entry.sysctl
        self.kind: SysctlKind = entry.kind

    @property
    def sysctl_path(self) -> Path:
        return sysctl_name_to_path(self.sysctl_name)

    def supports(self, summary: dict[str, Any]) -> bool:
        del summary
        try:
            return self.sysctl_path.is_file()
        except OSError:
            return False

    def apply(self, action: TunerAction, dry_run: bool = False) -> AppliedAction:
        path = self.sysctl_path
        previous = read_sysctl(path, self.kind)
        if not dry_run:
            write_sysctl(path, action.value, self.kind)
        return AppliedAction(
            action=action,
            previous_value=previous,
            metadata={"dry_run": dry_run, "sysctl": self.sysctl_name},
        )

    def rollback(self, applied: AppliedAction, dry_run: bool = False) -> bool:
        if dry_run:
            return True
        write_sysctl(self.sysctl_path, applied.previous_value, self.kind)
        return True


class BootCmdlineTuner(BaseTuner):
    """Observability-only tuner for boot cmdline keys; apply does not change runtime kernel."""

    def __init__(self, entry: TunerCatalogEntry, boot_params: dict[str, str | None]) -> None:
        if entry.scope != "boot_cmdline":
            raise ValueError("BootCmdlineTuner requires boot_cmdline scope")
        self._entry = entry
        self.tuner_id = entry.id
        self._boot_params = boot_params

    def supports(self, summary: dict[str, Any]) -> bool:
        del summary
        return self._entry.cmdline_key is not None

    def apply(self, action: TunerAction, dry_run: bool = False) -> AppliedAction:
        key = self._entry.cmdline_key or ""
        prev = self._boot_params.get(key)
        if prev is None and self._entry.default_cmdline_value is not None:
            prev = self._entry.default_cmdline_value
        return AppliedAction(
            action=action,
            previous_value=prev,
            metadata={
                "dry_run": dry_run,
                "requires_reboot": True,
                "effective": False,
                "cmdline_key": key,
            },
        )

    def rollback(self, applied: AppliedAction, dry_run: bool = False) -> bool:
        del applied, dry_run
        return True


def build_tuner_for_entry(
    entry: TunerCatalogEntry,
    boot_params: dict[str, str | None],
) -> BaseTuner:
    if entry.scope == "runtime_sysctl":
        return GenericSysctlTuner(entry)
    if entry.scope == "boot_cmdline":
        return BootCmdlineTuner(entry, boot_params)
    raise ValueError(f"unknown scope: {entry.scope}")
