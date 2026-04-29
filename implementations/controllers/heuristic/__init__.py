from __future__ import annotations

from typing import Any

from daemon_core.tuners import TunerAction, TunerRegistry
from daemon_core.tuners.sysctl_util import read_sysctl, sysctl_name_to_path
from daemon_core.types import AggregatorSample, ControllerRunContext
from implementations.executors import BatchTunerExecutor


def _summary_from_sample(sample: AggregatorSample) -> dict[str, Any] | None:
    if isinstance(sample.sample, dict):
        return sample.sample
    return None


class HeuristicController:
    def __init__(
        self,
        registry: TunerRegistry,
        *,
        low_mem_ratio: float = 0.15,
        high_mem_ratio: float = 0.35,
        high_dirty_kb: float = 50_000.0,
        low_dirty_kb: float = 1_000.0,
        max_history: int = 60,
    ) -> None:
        self.registry = registry
        self.low_mem_ratio = low_mem_ratio
        self.high_mem_ratio = high_mem_ratio
        self.high_dirty_kb = high_dirty_kb
        self.low_dirty_kb = low_dirty_kb
        self.history: list[dict[str, Any]] = []
        self.max_history = max_history

    async def accept_data(self, sample: AggregatorSample) -> None:
        summary = _summary_from_sample(sample)
        if summary is None:
            return
        self.history.append(summary)
        self.history = self.history[-self.max_history :]

    def _read_int(self, sysctl_name: str) -> int | None:
        try:
            return int(read_sysctl(sysctl_name_to_path(sysctl_name), "int"))
        except OSError:
            return None

    def propose(self, summary: dict[str, Any]) -> list[TunerAction]:
        host = summary.get("host_features", {})
        mem_avail = float(host.get("host_mem_available_ratio", 1.0))
        swap_free = float(host.get("host_swap_free_ratio", 1.0))
        dirty_kb = float(host.get("host_dirty_kb", 0.0))
        actions: list[TunerAction] = []

        if self.registry.is_enabled("sysctl_vm_swappiness"):
            tuner = self.registry.get("sysctl_vm_swappiness")
            if tuner is not None and tuner.supports(summary):
                entry = tuner._entry
                current = self._read_int("vm.swappiness")
                if current is not None and entry.min_value is not None and entry.max_value is not None:
                    if mem_avail <= self.low_mem_ratio and swap_free > 0.2 and current < entry.max_value:
                        actions.append(TunerAction(
                            tuner_id="sysctl_vm_swappiness",
                            action_id="increase_swappiness",
                            target="vm.swappiness",
                            value=min(int(entry.max_value), current + int(entry.step)),
                            reason="Low memory; bias reclaim toward swap.",
                            priority=50,
                            metadata={"current": current, "mem_available_ratio": mem_avail},
                        ))
                    elif mem_avail >= self.high_mem_ratio and current > entry.min_value:
                        actions.append(TunerAction(
                            tuner_id="sysctl_vm_swappiness",
                            action_id="decrease_swappiness",
                            target="vm.swappiness",
                            value=max(int(entry.min_value), current - int(entry.step)),
                            reason="Healthy free memory; reduce swap aggressiveness.",
                            priority=40,
                            metadata={"current": current, "mem_available_ratio": mem_avail},
                        ))

        if self.registry.is_enabled("sysctl_vm_dirty_ratio"):
            tuner = self.registry.get("sysctl_vm_dirty_ratio")
            if tuner is not None and tuner.supports(summary):
                entry = tuner._entry
                current = self._read_int("vm.dirty_ratio")
                if current is not None and entry.min_value is not None and entry.max_value is not None:
                    if dirty_kb > self.high_dirty_kb and current > entry.min_value:
                        actions.append(TunerAction(
                            tuner_id="sysctl_vm_dirty_ratio",
                            action_id="decrease_dirty_ratio",
                            target="vm.dirty_ratio",
                            value=max(int(entry.min_value), current - int(entry.step)),
                            reason="High dirty memory; trigger writeback sooner.",
                            priority=45,
                            metadata={"current": current, "dirty_kb": dirty_kb},
                        ))
                    elif dirty_kb < self.low_dirty_kb and current < entry.max_value:
                        actions.append(TunerAction(
                            tuner_id="sysctl_vm_dirty_ratio",
                            action_id="increase_dirty_ratio",
                            target="vm.dirty_ratio",
                            value=min(int(entry.max_value), current + int(entry.step)),
                            reason="Low dirty memory; allow more buffering before writeback.",
                            priority=35,
                            metadata={"current": current, "dirty_kb": dirty_kb},
                        ))

        if self.registry.is_enabled("sysctl_vm_vfs_cache_pressure"):
            tuner = self.registry.get("sysctl_vm_vfs_cache_pressure")
            if tuner is not None and tuner.supports(summary):
                entry = tuner._entry
                current = self._read_int("vm.vfs_cache_pressure")
                if current is not None and entry.min_value is not None and entry.max_value is not None:
                    if mem_avail < 0.20 and current < entry.max_value:
                        actions.append(TunerAction(
                            tuner_id="sysctl_vm_vfs_cache_pressure",
                            action_id="increase_cache_pressure",
                            target="vm.vfs_cache_pressure",
                            value=min(int(entry.max_value), current + int(entry.step)),
                            reason="Low memory; reclaim inode/dentry cache more aggressively.",
                            priority=48,
                            metadata={"current": current, "mem_available_ratio": mem_avail},
                        ))
                    elif mem_avail > 0.50 and current > entry.min_value:
                        actions.append(TunerAction(
                            tuner_id="sysctl_vm_vfs_cache_pressure",
                            action_id="decrease_cache_pressure",
                            target="vm.vfs_cache_pressure",
                            value=max(int(entry.min_value), current - int(entry.step)),
                            reason="Healthy memory; keep inode/dentry cache warmer.",
                            priority=38,
                            metadata={"current": current, "mem_available_ratio": mem_avail},
                        ))
        return actions

    async def run(self, ctx: ControllerRunContext) -> None:
        if not self.history:
            await ctx.log_decision("heuristic", "no summaries available", {})
            return
        summary = self.history[-1]
        actions = self.propose(summary)
        await ctx.log_decision(
            "heuristic",
            "heuristic proposal pass complete",
            {"actions": len(actions), "trigger_reason": ctx.trigger.reason},
        )
        sorted_actions = sorted(actions, key=lambda a: a.priority, reverse=True)
        if sorted_actions:
            await ctx.enqueue_executor(
                BatchTunerExecutor(self.registry, sorted_actions),
                {
                    "controller": "heuristic",
                    "action_count": len(sorted_actions),
                    "priority": sorted_actions[0].priority,
                    "tuner_ids": [action.tuner_id for action in sorted_actions],
                },
            )
