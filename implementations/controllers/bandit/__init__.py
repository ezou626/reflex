from __future__ import annotations

import json
import random
from pathlib import Path
from typing import Any

import numpy as np

from daemon_core.tuners import TunerRegistry
from daemon_core.types import AggregatorSample, ControllerRunContext
from implementations.controllers.tuning_shared import (
    ActionCandidate,
    PendingAction,
    RewardResult,
    build_step_candidate,
    canonical_decision_metadata,
    default_reward_path,
    dump_state,
    eligible_tuners,
    load_reward_metrics,
    load_state,
    noop_candidate,
    smoothed_reward,
    summary_from_sample,
    summary_metric,
)
from implementations.controllers.workload_classifier import DEFAULT_LIBRARY_PATH
from implementations.executors import BatchTunerExecutor


class ContextualBanditController:
    def __init__(
        self,
        registry: TunerRegistry,
        *,
        alpha: float = 0.05,
        epsilon: float = 0.10,
        evaluate_after_windows: int = 3,
        state_path: Path | None = None,
        reward_path: Path | None = None,
        reward_window: int = 3,
        baseline_windows: int = 3,
        max_steps_per_run: int = 1,
        library_path: Path | None = None,
        rng: random.Random | None = None,
        max_history: int = 120,
    ) -> None:
        self.registry = registry
        self.alpha = max(0.0, alpha)
        self.epsilon = max(0.0, min(epsilon, 1.0))
        self.evaluate_after_windows = max(1, evaluate_after_windows)
        self.state_path = state_path
        self.reward_metrics = load_reward_metrics(reward_path or default_reward_path())
        self.reward_window = max(1, reward_window)
        self.baseline_windows = max(1, baseline_windows)
        self.max_steps_per_run = max(1, max_steps_per_run)
        self.feature_keys, self.feature_norms = _load_feature_schema(library_path or DEFAULT_LIBRARY_PATH)
        self.rng = rng or random.Random()
        self.max_history = max_history
        self.history: list[tuple[int, dict[str, Any]]] = []
        self.pending: PendingAction | None = None
        self._pending_action_key: str | None = None
        self._pending_context: list[float] | None = None
        self._weights: dict[str, np.ndarray] = {}
        self._load_state()

    async def accept_data(self, sample: AggregatorSample) -> None:
        summary = summary_from_sample(sample)
        if summary is None:
            return
        self.history.append((sample.id, summary))
        self.history = self.history[-self.max_history :]

    def _latest_sample_id(self) -> int:
        return self.history[-1][0] if self.history else 0

    def _summaries(self) -> list[dict[str, Any]]:
        return [summary for _, summary in self.history]

    def _reward(self) -> RewardResult:
        return smoothed_reward(self._summaries(), self.reward_metrics, self.reward_window)

    def _reward_after_pending(self, pending: PendingAction) -> RewardResult:
        summaries = [
            summary
            for sample_id, summary in self.history
            if sample_id > pending.applied_at_sample_id
        ]
        return smoothed_reward(summaries, self.reward_metrics, self.reward_window)

    def _current_summary(self) -> dict[str, Any]:
        return self.history[-1][1]

    def _context(self) -> np.ndarray:
        if not self.feature_keys:
            return np.array([1.0], dtype=np.float64)
        vals = [
            max(0.0, min(summary_metric(self._current_summary(), key) / max(norm, 1e-9), 1.0))
            for key, norm in zip(self.feature_keys, self.feature_norms)
        ]
        vals.append(1.0)
        return np.array(vals, dtype=np.float64)

    def _action_space(self) -> dict[str, ActionCandidate]:
        out: dict[str, ActionCandidate] = {"noop": noop_candidate("no improvement expected")}
        for tuner in eligible_tuners(self.registry, self._current_summary()):
            inc = build_step_candidate(tuner, "increase", reason="bandit one-step increase", priority=55)
            dec = build_step_candidate(tuner, "decrease", reason="bandit one-step decrease", priority=55)
            if inc.action is not None:
                out[f"{inc.action.tuner_id}:increase"] = inc
            if dec.action is not None:
                out[f"{dec.action.tuner_id}:decrease"] = dec
        return out

    def _weights_for(self, key: str, width: int) -> np.ndarray:
        weights = self._weights.get(key)
        if weights is None or len(weights) != width:
            weights = np.zeros(width, dtype=np.float64)
            self._weights[key] = weights
        return weights

    def _select(self, actions: dict[str, ActionCandidate], context: np.ndarray) -> tuple[str, ActionCandidate]:
        keys = sorted(actions)
        if self.rng.random() < self.epsilon:
            key = self.rng.choice(keys)
            return key, actions[key]
        scored = [
            (float(np.dot(self._weights_for(key, len(context)), context)), key)
            for key in keys
        ]
        _, key = max(scored)
        return key, actions[key]

    async def run(self, ctx: ControllerRunContext) -> None:
        if not self.history:
            await ctx.log_decision("bandit", "no summaries available", {})
            return
        if len(self.history) < self.baseline_windows:
            reward = self._reward()
            await ctx.log_decision(
                "bandit",
                "collecting initial baseline",
                canonical_decision_metadata(
                    controller="bandit",
                    candidate=noop_candidate("initial baseline"),
                    reward_before=reward,
                    extra={"baseline_windows": self.baseline_windows},
                ),
            )
            return
        if self.pending is not None:
            await self._evaluate_pending(ctx)
            return

        actions = self._action_space()
        context = self._context()
        action_key, candidate = self._select(actions, context)
        reward_before = self._reward()
        latest_id = self._latest_sample_id()
        self.pending = PendingAction(
            tuner_id=candidate.action.tuner_id if candidate.action is not None else None,
            action=candidate.action_name,
            prev_value=candidate.current_value,
            new_value=candidate.candidate_value,
            reward_before=reward_before.total_reward,
            applied_at_sample_id=latest_id,
            evaluation_due_at_sample_id=latest_id + self.evaluate_after_windows,
        )
        self._pending_action_key = action_key
        self._pending_context = context.tolist()
        await ctx.log_decision(
            "bandit",
            "bandit selected action",
            canonical_decision_metadata(
                controller="bandit",
                candidate=candidate,
                reward_before=reward_before,
                pending_action=self.pending,
                extra={"action_key": action_key, "context": context.tolist()},
            ),
        )
        if candidate.action is not None:
            await ctx.enqueue_executor(
                BatchTunerExecutor(self.registry, [candidate.action]),
                {
                    "controller": "bandit",
                    "action_count": 1,
                    "tuner_ids": [candidate.action.tuner_id],
                },
            )
        self._save_state()

    async def _evaluate_pending(self, ctx: ControllerRunContext) -> None:
        assert self.pending is not None
        if self._latest_sample_id() < self.pending.evaluation_due_at_sample_id:
            await ctx.log_decision(
                "bandit",
                "pending action awaiting evaluation",
                canonical_decision_metadata(
                    controller="bandit",
                    candidate=noop_candidate("pending evaluation"),
                    reward_before=self._reward(),
                    pending_action=self.pending,
                ),
            )
            return
        reward_after = self._reward_after_pending(self.pending)
        context = np.array(self._pending_context or [1.0], dtype=np.float64)
        action_key = self._pending_action_key or "noop"
        weights = self._weights_for(action_key, len(context))
        predicted = float(np.dot(weights, context))
        target = reward_after.total_reward
        weights += self.alpha * (target - predicted) * context
        candidate = ActionCandidate(
            action=None,
            direction="noop",
            current_value=self.pending.prev_value,
            candidate_value=self.pending.new_value,
            reason="bandit reward update",
        )
        accepted = reward_after.total_reward >= self.pending.reward_before
        await ctx.log_decision(
            "bandit",
            "bandit evaluation complete",
            canonical_decision_metadata(
                controller="bandit",
                candidate=candidate,
                reward_before=RewardResult(self.pending.reward_before, {}),
                reward_after=reward_after,
                accepted=accepted,
                pending_action=self.pending,
                extra={
                    "action_key": action_key,
                    "prediction_before_update": predicted,
                    "alpha": self.alpha,
                },
            ),
        )
        self.pending = None
        self._pending_action_key = None
        self._pending_context = None
        self._save_state()

    def _load_state(self) -> None:
        if self.state_path is None:
            return
        raw = load_state(self.state_path)
        weights = raw.get("weights", {})
        if isinstance(weights, dict):
            self._weights = {
                str(k): np.array(v, dtype=np.float64)
                for k, v in weights.items()
                if isinstance(v, list)
            }

    def _save_state(self) -> None:
        if self.state_path is None:
            return
        dump_state(
            self.state_path,
            {
                "weights": {k: v.tolist() for k, v in self._weights.items()},
                "pending_action": self.pending.log_payload() if self.pending else None,
                "pending_action_key": self._pending_action_key,
                "pending_context": self._pending_context,
            },
        )


def _load_feature_schema(path: Path) -> tuple[list[str], list[float]]:
    if not path.is_file():
        return [], []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return [], []
    if not isinstance(raw, dict):
        return [], []
    for entry in raw.values():
        if not isinstance(entry, dict):
            continue
        keys = entry.get("feature_keys")
        norms = entry.get("feature_norms")
        if isinstance(keys, list) and isinstance(norms, list) and len(keys) == len(norms):
            return [str(k) for k in keys], [float(n) for n in norms]
    return [], []


__all__ = ["ContextualBanditController"]
