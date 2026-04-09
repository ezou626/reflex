from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from tuners.base import TunerAction


def _parse_csv(value: str) -> list[str]:
    return [part.strip() for part in value.split(",") if part.strip()]


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

    @classmethod
    def from_file(cls, path: Path) -> "TuningPolicy":
        policy = cls()
        if not path.is_file():
            return policy
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if ":" not in line:
                continue
            key, value = [x.strip() for x in line.split(":", 1)]
            if key == "compare_metrics":
                policy.compare_metrics = _parse_csv(value)
            elif key == "min_windows_before_action":
                policy.min_windows_before_action = int(value)
            elif key == "cooldown_windows":
                policy.cooldown_windows = int(value)
            elif key == "evaluate_after_windows":
                policy.evaluate_after_windows = int(value)
            elif key == "regression_threshold":
                policy.regression_threshold = float(value)
            elif key == "improvement_threshold":
                policy.improvement_threshold = float(value)
        return policy


@dataclass
class Decision:
    trigger: str
    reason: str
    candidate_actions: list[dict[str, Any]]
    chosen_action: TunerAction | None


class DecisionEngine:
    def __init__(self, policy: TuningPolicy) -> None:
        self.policy = policy
        self._cooldown_remaining = 0

    def note_rollback(self) -> None:
        self._cooldown_remaining = max(self._cooldown_remaining, self.policy.cooldown_windows)

    def decide(
        self,
        trigger: str,
        summary: dict[str, Any],
        history: list[dict[str, Any]],
        proposals: list[TunerAction],
    ) -> Decision:
        if len(history) < self.policy.min_windows_before_action:
            return Decision(
                trigger=trigger,
                reason="insufficient_history",
                candidate_actions=[_action_to_dict(x) for x in proposals],
                chosen_action=None,
            )
        if self._cooldown_remaining > 0:
            self._cooldown_remaining -= 1
            return Decision(
                trigger=trigger,
                reason="cooldown_active",
                candidate_actions=[_action_to_dict(x) for x in proposals],
                chosen_action=None,
            )
        if not proposals:
            return Decision(
                trigger=trigger,
                reason="no_action_proposed",
                candidate_actions=[],
                chosen_action=None,
            )

        chosen = sorted(proposals, key=lambda x: x.priority, reverse=True)[0]
        self._cooldown_remaining = self.policy.cooldown_windows
        del summary
        return Decision(
            trigger=trigger,
            reason="selected_highest_priority_action",
            candidate_actions=[_action_to_dict(x) for x in proposals],
            chosen_action=chosen,
        )


def _action_to_dict(action: TunerAction) -> dict[str, Any]:
    return {
        "tuner_id": action.tuner_id,
        "action_id": action.action_id,
        "target": action.target,
        "value": action.value,
        "reason": action.reason,
        "priority": action.priority,
        "metadata": action.metadata,
    }
