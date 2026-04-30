from __future__ import annotations

from pathlib import Path
from typing import Any

from reflex.core.tuners.base import AppliedAction, BaseTuner, TunerAction
from reflex.core.tuners.schema import SysctlKind, TunerCatalogEntry
from reflex.core.tuners.sysctl_util import read_sysctl, sysctl_name_to_path, write_sysctl


class GenericSysctlTuner(BaseTuner):
    def __init__(
        self,
        entry: TunerCatalogEntry,
        *,
        sysctl_root: Path = Path("/proc/sys"),
    ) -> None:
        if entry.scope != "runtime_sysctl":
            raise ValueError("GenericSysctlTuner requires runtime_sysctl scope")
        self._entry = entry
        self.tuner_id = entry.id
        self.sysctl_name = entry.sysctl
        self.kind: SysctlKind = entry.kind
        self._sysctl_root = sysctl_root

    @property
    def sysctl_path(self) -> Path:
        return self._sysctl_root.joinpath(*self.sysctl_name.split("."))

    def supports(self, sample: Any) -> bool:
        del sample
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


def build_tuner_for_entry(entry: TunerCatalogEntry) -> BaseTuner:
    if entry.scope == "runtime_sysctl":
        return GenericSysctlTuner(entry)
    raise ValueError(f"unknown scope: {entry.scope}")
