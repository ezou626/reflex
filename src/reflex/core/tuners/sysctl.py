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

    def supports(self) -> bool:
        try:
            return self.sysctl_path.is_file()
        except OSError:
            return False

    def _validate_int_value(self, value: Any) -> int | None:
        try:
            int_value = int(value)
        except (TypeError, ValueError):
            return None
        if self._entry.min_value is not None and int_value < int(self._entry.min_value):
            return None
        if self._entry.max_value is not None and int_value > int(self._entry.max_value):
            return None
        if self._entry.min_value is not None and self._entry.step not in (None, 0):
            min_value = int(self._entry.min_value)
            step = int(self._entry.step)
            if step > 0 and (int_value - min_value) % step != 0:
                return None
        return int_value

    def create_step_action(
        self,
        direction: str,
        *,
        steps: int = 1,
        reason: str,
        metadata: dict[str, Any] | None = None,
    ) -> TunerAction | None:
        current = self._read_current_int()
        if current is None or self._entry.step is None:
            return None
        delta = int(self._entry.step) * max(1, int(steps))
        if direction == "increase":
            candidate = current + delta
        elif direction == "decrease":
            candidate = current - delta
        else:
            return None
        validated = self._validate_int_value(candidate)
        if validated is None or validated == current:
            return None
        return TunerAction(
            tuner_id=self.tuner_id,
            action_id=f"{direction}_{self.tuner_id}",
            target=self.sysctl_name,
            value=validated,
            reason=reason,
            metadata={
                **(metadata or {}),
                "current": current,
                "direction": direction,
                "steps": max(1, int(steps)),
            },
        )

    def create_set_action(
        self,
        value: Any,
        *,
        action_id: str,
        reason: str,
        metadata: dict[str, Any] | None = None,
    ) -> TunerAction | None:
        validated = self._validate_int_value(value) if self.kind == "int" else None
        if validated is None:
            return None
        return TunerAction(
            tuner_id=self.tuner_id,
            action_id=action_id,
            target=self.sysctl_name,
            value=validated,
            reason=reason,
            metadata=metadata or {},
        )

    def _read_current_int(self) -> int | None:
        try:
            return int(read_sysctl(self.sysctl_path, "int"))
        except (OSError, ValueError, TypeError):
            return None

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
