from __future__ import annotations

from pathlib import Path
from typing import Any

from tuners.base import AppliedAction, BaseTuner, TunerAction


class SysctlSwappinessTuner(BaseTuner):
    tuner_id = "sysctl_swappiness"

    def __init__(
        self,
        min_value: int = 10,
        max_value: int = 90,
        step: int = 5,
        low_mem_ratio: float = 0.15,
        high_mem_ratio: float = 0.35,
    ) -> None:
        self.min_value = min_value
        self.max_value = max_value
        self.step = step
        self.low_mem_ratio = low_mem_ratio
        self.high_mem_ratio = high_mem_ratio

    @property
    def _sysctl_path(self) -> Path:
        return Path("/proc/sys/vm/swappiness")

    def _read_swappiness(self) -> int:
        return int(self._sysctl_path.read_text(encoding="utf-8").strip())

    def _write_swappiness(self, value: int) -> None:
        self._sysctl_path.write_text(f"{value}\n", encoding="utf-8")

    def supports(self, summary: dict[str, Any]) -> bool:
        host = summary.get("host_features", {})
        return "host_mem_available_ratio" in host

    def propose(
        self,
        summary: dict[str, Any],
        history: list[dict[str, Any]],
    ) -> list[TunerAction]:
        del history
        host = summary.get("host_features", {})
        mem_avail = float(host.get("host_mem_available_ratio", 1.0))
        swap_free = float(host.get("host_swap_free_ratio", 1.0))
        current = self._read_swappiness()

        actions: list[TunerAction] = []
        if mem_avail <= self.low_mem_ratio and swap_free > 0.2 and current < self.max_value:
            new_value = min(self.max_value, current + self.step)
            actions.append(
                TunerAction(
                    tuner_id=self.tuner_id,
                    action_id="increase_swappiness",
                    target="vm.swappiness",
                    value=new_value,
                    reason="Low memory availability; bias reclaim toward swap.",
                    priority=50,
                    metadata={"current": current, "mem_available_ratio": mem_avail},
                )
            )
        elif mem_avail >= self.high_mem_ratio and current > self.min_value:
            new_value = max(self.min_value, current - self.step)
            actions.append(
                TunerAction(
                    tuner_id=self.tuner_id,
                    action_id="decrease_swappiness",
                    target="vm.swappiness",
                    value=new_value,
                    reason="Healthy free memory; reduce swap aggressiveness.",
                    priority=40,
                    metadata={"current": current, "mem_available_ratio": mem_avail},
                )
            )
        return actions

    def apply(self, action: TunerAction, dry_run: bool = False) -> AppliedAction:
        previous = self._read_swappiness()
        if not dry_run:
            self._write_swappiness(int(action.value))
        return AppliedAction(
            action=action,
            previous_value=previous,
            metadata={"dry_run": dry_run},
        )

    def rollback(self, applied: AppliedAction, dry_run: bool = False) -> bool:
        if dry_run:
            return True
        self._write_swappiness(int(applied.previous_value))
        return True
