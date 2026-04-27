from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from config.schema import DecisionSelectionMode, TuningPolicy

from tuners.base import TunerAction


@dataclass
class Decision:
    trigger: str
    reason: str
    candidate_actions: list[dict[str, Any]]
    chosen_actions: list[TunerAction]


class DecisionEngine:
    def __init__(self, policy: TuningPolicy) -> None:
        self.policy = policy
        self._cooldown_remaining = 0

    def note_rollback(self) -> None:
        self._cooldown_remaining = max(
            self._cooldown_remaining, self.policy.cooldown_windows
        )

    def decide(
        self,
        trigger: str,
        summary: dict[str, Any],
        history: list[dict[str, Any]],
        proposals: list[TunerAction],
    ) -> Decision:
        del summary
        if len(history) < self.policy.min_windows_before_action:
            return Decision(
                trigger=trigger,
                reason="insufficient_history",
                candidate_actions=[_action_to_dict(x) for x in proposals],
                chosen_actions=[],
            )
        if self._cooldown_remaining > 0:
            self._cooldown_remaining -= 1
            return Decision(
                trigger=trigger,
                reason="cooldown_active",
                candidate_actions=[_action_to_dict(x) for x in proposals],
                chosen_actions=[],
            )
        if not proposals:
            return Decision(
                trigger=trigger,
                reason="no_action_proposed",
                candidate_actions=[],
                chosen_actions=[],
            )

        chosen = select_actions(proposals, self.policy)
        self._cooldown_remaining = self.policy.cooldown_windows
        return Decision(
            trigger=trigger,
            reason="selected_actions_by_policy",
            candidate_actions=[_action_to_dict(x) for x in proposals],
            chosen_actions=chosen,
        )


def select_actions(proposals: list[TunerAction], policy: TuningPolicy) -> list[TunerAction]:
    if not proposals:
        return []
    mode: DecisionSelectionMode = policy.decision_selection_mode
    cap = max(1, policy.max_actions_per_tick)
    sorted_p = sorted(proposals, key=lambda x: x.priority, reverse=True)

    if mode == "top_priority_only":
        out = sorted_p[:1]
    elif mode == "top_n_by_priority":
        out = sorted_p[:cap]
    elif mode == "priority_floor":
        out = [p for p in sorted_p if p.priority >= policy.priority_floor][:cap]
    elif mode == "all_unique_targets":
        seen: set[str] = set()
        out = []
        for p in sorted_p:
            if p.target in seen:
                continue
            seen.add(p.target)
            out.append(p)
            if len(out) >= cap:
                break
    else:
        out = sorted_p[:1]

    if policy.dedupe_by_target:
        seen2: set[str] = set()
        deduped: list[TunerAction] = []
        for p in out:
            if p.target in seen2:
                continue
            seen2.add(p.target)
            deduped.append(p)
        out = deduped

    if policy.min_priority_gap > 0 and len(out) > 1:
        filtered = [out[0]]
        for p in out[1:]:
            if filtered[-1].priority - p.priority >= policy.min_priority_gap:
                filtered.append(p)
        out = filtered

    return out[:cap]


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
