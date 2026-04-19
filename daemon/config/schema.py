from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

TunerScope = Literal["runtime_sysctl", "boot_cmdline"]
SysctlKind = Literal["int", "ints", "str"]
DecisionSelectionMode = Literal[
    "top_priority_only",
    "top_n_by_priority",
    "priority_floor",
    "all_unique_targets",
]
RollbackBatchGranularity = Literal["per_frame", "per_window_batch"]


@dataclass
class TuningPolicy:
    compare_metrics: list[str] = field(
        default_factory=lambda: [
            "context_switch_rate_per_sec",
            "syscall_error_rate",
            "rq_latency_p95_us",
            "rq_latency_p99_us",
            "host_cpu_busy_ratio",
            "host_mem_available_ratio",
        ]
    )
    min_windows_before_action: int = 3
    cooldown_windows: int = 2
    evaluate_after_windows: int = 3
    regression_threshold: float = 0.05
    improvement_threshold: float = 0.02
    decision_selection_mode: DecisionSelectionMode = "top_priority_only"
    max_actions_per_tick: int = 1
    min_priority_gap: int = 0
    priority_floor: int = 0
    dedupe_by_target: bool = False
    rollback_batch_granularity: RollbackBatchGranularity = "per_frame"
    max_tracked_applies: int = 64


@dataclass
class TunerCatalogEntry:
    id: str
    category: str
    description: str
    kind: SysctlKind
    scope: TunerScope = "runtime_sysctl"
    sysctl: str = ""
    cmdline_key: str | None = None
    default_cmdline_value: str | None = None
    enabled: bool = True
    tags: list[str] = field(default_factory=list)
    min_value: int | float | None = None
    max_value: int | float | None = None
    step: int | float = 1


@dataclass
class TunerCatalogDoc:
    version: int = 1
    tuners: list[TunerCatalogEntry] = field(default_factory=list)
