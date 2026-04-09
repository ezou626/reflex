from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from decision_engine import TuningPolicy
from history import WindowHistory
from tuners.base import AppliedAction


@dataclass
class RollbackResult:
    should_rollback: bool
    reason: str
    effects: dict[str, float]


class RollbackManager:
    def __init__(self, policy: TuningPolicy) -> None:
        self.policy = policy

    def evaluate(
        self,
        active: AppliedAction,
        history: WindowHistory,
    ) -> RollbackResult:
        recent = history.latest(self.policy.evaluate_after_windows + 1)
        if len(recent) < self.policy.evaluate_after_windows + 1:
            return RollbackResult(
                should_rollback=False,
                reason="awaiting_more_windows",
                effects={},
            )

        before = recent[0]
        after = recent[-1]
        effects = _effect_size(before, after, self.policy.compare_metrics)
        regressions = [
            key
            for key, value in effects.items()
            if key != "host_mem_available_ratio" and value > self.policy.regression_threshold
        ]
        if regressions:
            return RollbackResult(
                should_rollback=True,
                reason=(
                    f"regression_detected_after_action:{active.action.action_id}:"
                    + ",".join(regressions)
                ),
                effects=effects,
            )
        return RollbackResult(should_rollback=False, reason="no_regression", effects=effects)


def _value_for_key(summary: dict[str, Any], key: str) -> float:
    if key in summary.get("metrics", {}):
        return float(summary["metrics"][key])
    if key in summary.get("host_features", {}):
        return float(summary["host_features"][key])
    return 0.0


def _effect_size(
    before: dict[str, Any],
    after: dict[str, Any],
    keys: list[str],
) -> dict[str, float]:
    effects: dict[str, float] = {}
    for key in keys:
        prev = _value_for_key(before, key)
        curr = _value_for_key(after, key)
        denom = abs(prev) if abs(prev) > 1e-9 else 1.0
        effects[key] = (curr - prev) / denom
    return effects
