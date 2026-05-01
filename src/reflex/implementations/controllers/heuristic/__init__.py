from __future__ import annotations

from typing import Any

from reflex.core.tuners import TunerAction, TunerRegistry
from reflex.core.tuners.sysctl_util import read_sysctl, sysctl_name_to_path
from reflex.core.types import AggregatorSample, ControllerRunContext
from reflex.implementations.executors import BatchTunerExecutor


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
    ) -> None:
        self.registry = registry
        self.low_mem_ratio = low_mem_ratio
        self.high_mem_ratio = high_mem_ratio
        self.high_dirty_kb = high_dirty_kb
        self.low_dirty_kb = low_dirty_kb
        self.current_summary: dict[str, Any] | None = None

    async def accept_data(self, sample: AggregatorSample) -> None:
        summary = _summary_from_sample(sample)
        if summary is None:
            return
        self.current_summary = summary

    def _decision_snapshot(self, summary: dict[str, Any]) -> dict[str, Any]:
        host = summary.get("host_features", {})
        metrics = summary.get("metrics", {})
        return {
            "metrics": {
                "process_churn_rate_per_sec": metrics.get("process_churn_rate_per_sec"),
                "context_switch_rate_per_sec": metrics.get("context_switch_rate_per_sec"),
                "syscall_latency_p95_us": metrics.get("syscall_latency_p95_us"),
                "syscall_error_rate": metrics.get("syscall_error_rate"),
                "syscall_error_rate_per_sec": metrics.get("syscall_error_rate_per_sec"),
                "rq_latency_p95_us": metrics.get("rq_latency_p95_us"),
                "direct_reclaim_rate_per_sec": metrics.get("direct_reclaim_rate_per_sec"),
                "blk_latency_p95_us": metrics.get("blk_latency_p95_us"),
            },
            "host_features": {
                "host_mem_available_ratio": host.get("host_mem_available_ratio"),
                "host_swap_free_ratio": host.get("host_swap_free_ratio"),
                "host_dirty_kb": host.get("host_dirty_kb"),
                "host_cpu_busy_ratio": host.get("host_cpu_busy_ratio"),
            },
            "sysctls": {
                "vm.swappiness": self._read_int("vm.swappiness"),
                "vm.dirty_ratio": self._read_int("vm.dirty_ratio"),
                "vm.vfs_cache_pressure": self._read_int("vm.vfs_cache_pressure"),
            },
            "thresholds": {
                "low_mem_ratio": self.low_mem_ratio,
                "high_mem_ratio": self.high_mem_ratio,
                "high_dirty_kb": self.high_dirty_kb,
                "low_dirty_kb": self.low_dirty_kb,
            },
        }

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
            if tuner is not None and tuner.supports():
                if mem_avail <= self.low_mem_ratio and swap_free > 0.2:
                    action = tuner.create_step_action(
                        "increase",
                        reason="Low memory; bias reclaim toward swap.",
                    )
                    if action is not None:
                        actions.append(action)
                elif mem_avail >= self.high_mem_ratio:
                    action = tuner.create_step_action(
                        "decrease",
                        reason="Healthy free memory; reduce swap aggressiveness.",
                    )
                    if action is not None:
                        actions.append(action)

        if self.registry.is_enabled("sysctl_vm_dirty_ratio"):
            tuner = self.registry.get("sysctl_vm_dirty_ratio")
            if tuner is not None and tuner.supports():
                if dirty_kb > self.high_dirty_kb:
                    action = tuner.create_step_action(
                        "decrease",
                        reason="High dirty memory; trigger writeback sooner.",
                    )
                    if action is not None:
                        actions.append(action)
                elif dirty_kb < self.low_dirty_kb:
                    action = tuner.create_step_action(
                        "increase",
                        reason="Low dirty memory; allow more buffering before writeback.",
                    )
                    if action is not None:
                        actions.append(action)

        if self.registry.is_enabled("sysctl_vm_vfs_cache_pressure"):
            tuner = self.registry.get("sysctl_vm_vfs_cache_pressure")
            if tuner is not None and tuner.supports():
                if mem_avail < 0.20:
                    action = tuner.create_step_action(
                        "increase",
                        reason="Low memory; reclaim inode/dentry cache more aggressively.",
                    )
                    if action is not None:
                        actions.append(action)
                elif mem_avail > 0.50:
                    action = tuner.create_step_action(
                        "decrease",
                        reason="Healthy memory; keep inode/dentry cache warmer.",
                    )
                    if action is not None:
                        actions.append(action)
        return actions

    async def run(self, ctx: ControllerRunContext) -> None:
        if self.current_summary is None:
            await ctx.log_decision("heuristic", "no summaries available", {})
            return
        snapshot = self._decision_snapshot(self.current_summary)
        await ctx.log_decision(
            "heuristic",
            "heuristic input snapshot",
            snapshot,
        )
        actions = self.propose(self.current_summary)
        await ctx.log_decision(
            "heuristic",
            "heuristic proposal pass complete",
            {
                "actions": len(actions),
                "trigger_reason": ctx.trigger.reason,
            },
        )
        if actions:
            await ctx.enqueue_executor(
                BatchTunerExecutor(self.registry, actions),
                {
                    "controller": "heuristic",
                    "action_count": len(actions),
                    "tuner_ids": [action.tuner_id for action in actions],
                },
            )
