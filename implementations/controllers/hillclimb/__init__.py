from __future__ import annotations

import random
from pathlib import Path
from typing import Any

from daemon_core.tuners import TunerRegistry
from daemon_core.types import AggregatorSample, ControllerRunContext
from implementations.controllers.tuning_shared import (
    ActionCandidate,
    PendingAction,
    RewardResult,
    annealing_accept,
    build_set_action,
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
)
from implementations.executors import BatchTunerExecutor


class HillClimbController:
    def __init__(
        self,
        registry: TunerRegistry,
        *,
        interval_windows: int = 5,
        evaluate_after_windows: int = 3,
        temperature: float = 0.0,
        cooling: float = 0.95,
        epsilon: float = 0.10,
        reward_path: Path | None = None,
        reward_window: int = 3,
        baseline_windows: int = 3,
        delta_threshold: float = 0.01,
        max_steps_per_run: int = 1,
        state_path: Path | None = None,
        rng: random.Random | None = None,
        max_history: int = 120,
    ) -> None:
        self.registry = registry
        self.interval_windows = max(1, interval_windows)
        self.evaluate_after_windows = max(1, evaluate_after_windows)
        self.temperature = max(0.0, temperature)
        self.cooling = max(0.0, min(cooling, 1.0))
        self.epsilon = max(0.0, min(epsilon, 1.0))
        self.reward_metrics = load_reward_metrics(reward_path or default_reward_path())
        self.reward_window = max(1, reward_window)
        self.baseline_windows = max(1, baseline_windows)
        self.delta_threshold = max(0.0, delta_threshold)
        self.max_steps_per_run = max(1, max_steps_per_run)
        self.state_path = state_path
        self.rng = rng or random.Random()
        self.max_history = max_history
        self.history: list[tuple[int, dict[str, Any]]] = []
        self.pending: PendingAction | None = None
        self.best_reward: float | None = None
        self._decision_windows = 0
        self._cooldown_until_sample_id = 0
        self._tried_bins: dict[str, set[int]] = {}
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

    def _baseline_ready(self) -> bool:
        return len(self.history) >= self.baseline_windows

    def _current_summary(self) -> dict[str, Any]:
        return self.history[-1][1]

    def _bin_for(self, tuner_id: str, value: int) -> int:
        return int(value)

    def _choose_candidate(self) -> ActionCandidate:
        if self._latest_sample_id() < self._cooldown_until_sample_id:
            return noop_candidate("cooldown")
        tuners = eligible_tuners(self.registry, self._current_summary())
        candidates: list[ActionCandidate] = [noop_candidate("no improvement expected")]
        for tuner in tuners:
            candidates.append(
                build_step_candidate(
                    tuner,
                    "increase",
                    reason="hillclimb candidate increase",
                    priority=60,
                )
            )
            candidates.append(
                build_step_candidate(
                    tuner,
                    "decrease",
                    reason="hillclimb candidate decrease",
                    priority=60,
                )
            )
        actionable = [c for c in candidates if c.action is not None]
        if not actionable:
            return noop_candidate("unsafe read")
        if self.rng.random() < self.epsilon:
            return self.rng.choice(candidates)
        return max(candidates, key=self._candidate_score)

    def _candidate_score(self, candidate: ActionCandidate) -> float:
        if candidate.action is None:
            return 0.01
        tried = self._tried_bins.setdefault(candidate.action.tuner_id, set())
        value = candidate.candidate_value
        bonus = 1.0 if value is not None and self._bin_for(candidate.action.tuner_id, value) not in tried else 0.0
        return bonus + self.rng.random() * 0.001

    async def run(self, ctx: ControllerRunContext) -> None:
        if not self.history:
            await ctx.log_decision("hillclimb", "no summaries available", {})
            return
        if not self._baseline_ready():
            reward = self._reward()
            await ctx.log_decision(
                "hillclimb",
                "collecting initial baseline",
                canonical_decision_metadata(
                    controller="hillclimb",
                    candidate=noop_candidate("initial baseline"),
                    reward_before=reward,
                    extra={"baseline_windows": self.baseline_windows},
                ),
            )
            return
        if self.best_reward is None:
            self.best_reward = self._reward().total_reward

        if self.pending is not None:
            await self._evaluate_pending(ctx)
            return

        self._decision_windows += 1
        if self._decision_windows % self.interval_windows != 0:
            reward = self._reward()
            await ctx.log_decision(
                "hillclimb",
                "waiting for hillclimb interval",
                canonical_decision_metadata(
                    controller="hillclimb",
                    candidate=noop_candidate("cooldown"),
                    reward_before=reward,
                    extra={"interval_windows": self.interval_windows},
                ),
            )
            return

        reward_before = self._reward()
        candidate = _first_non_noop_if_over_cap(self._choose_candidate(), self.max_steps_per_run)
        if candidate.action is None:
            await ctx.log_decision(
                "hillclimb",
                "hillclimb selected no-op",
                canonical_decision_metadata(
                    controller="hillclimb",
                    candidate=candidate,
                    reward_before=reward_before,
                ),
            )
            self._save_state()
            return

        latest_id = self._latest_sample_id()
        due_id = latest_id + self.evaluate_after_windows
        rollback = None
        tuners = {t.tuner_id: t for t in eligible_tuners(self.registry, self._current_summary())}
        tuner = tuners.get(candidate.action.tuner_id)
        if tuner is not None and candidate.current_value is not None:
            rollback = build_set_action(
                tuner,
                candidate.current_value,
                action_id=f"rollback_{candidate.action.tuner_id}",
                reason="rollback regressed hillclimb candidate",
            )
        self.pending = PendingAction(
            tuner_id=candidate.action.tuner_id,
            action=candidate.action.action_id,
            prev_value=candidate.current_value,
            new_value=candidate.candidate_value,
            reward_before=reward_before.total_reward,
            applied_at_sample_id=latest_id,
            evaluation_due_at_sample_id=due_id,
            rollback_action=rollback,
        )
        await ctx.log_decision(
            "hillclimb",
            "hillclimb scheduled candidate",
            canonical_decision_metadata(
                controller="hillclimb",
                candidate=candidate,
                reward_before=reward_before,
                pending_action=self.pending,
            ),
        )
        await ctx.enqueue_executor(
            BatchTunerExecutor(self.registry, [candidate.action]),
            {
                "controller": "hillclimb",
                "action_count": 1,
                "tuner_ids": [candidate.action.tuner_id],
            },
        )
        self._save_state()

    async def _evaluate_pending(self, ctx: ControllerRunContext) -> None:
        assert self.pending is not None
        if self._latest_sample_id() < self.pending.evaluation_due_at_sample_id:
            await ctx.log_decision(
                "hillclimb",
                "pending action awaiting evaluation",
                canonical_decision_metadata(
                    controller="hillclimb",
                    candidate=noop_candidate("pending evaluation"),
                    reward_before=self._reward(),
                    pending_action=self.pending,
                ),
            )
            return
        reward_after = self._reward_after_pending(self.pending)
        delta = reward_after.total_reward - self.pending.reward_before
        accepted = delta >= -self.delta_threshold or annealing_accept(
            delta,
            self.temperature,
            self.rng,
        )
        if accepted:
            candidate = noop_candidate("accepted candidate")
        else:
            candidate = ActionCandidate(
                action=self.pending.rollback_action,
                direction="decrease",
                current_value=self.pending.new_value,
                candidate_value=self.pending.prev_value,
                reason="rollback triggered",
            )
        if accepted:
            self.best_reward = max(self.best_reward or reward_after.total_reward, reward_after.total_reward)
            if self.pending.tuner_id is not None and self.pending.new_value is not None:
                self._tried_bins.setdefault(self.pending.tuner_id, set()).add(
                    self._bin_for(self.pending.tuner_id, self.pending.new_value)
                )
            self._cooldown_until_sample_id = self._latest_sample_id() + 1
        await ctx.log_decision(
            "hillclimb",
            "hillclimb evaluation complete",
            canonical_decision_metadata(
                controller="hillclimb",
                candidate=candidate,
                reward_before=RewardResult(self.pending.reward_before, {}),
                reward_after=reward_after,
                accepted=accepted,
                pending_action=self.pending,
                extra={
                    "delta_threshold": self.delta_threshold,
                    "temperature": self.temperature,
                },
            ),
        )
        rollback = self.pending.rollback_action
        self.pending = None
        self.temperature *= self.cooling
        if not accepted and rollback is not None:
            await ctx.enqueue_executor(
                BatchTunerExecutor(self.registry, [rollback]),
                {
                    "controller": "hillclimb",
                    "action_count": 1,
                    "rollback": True,
                    "tuner_ids": [rollback.tuner_id],
                },
            )
        self._save_state()

    def _load_state(self) -> None:
        if self.state_path is None:
            return
        raw = load_state(self.state_path)
        self.best_reward = raw.get("best_reward")
        self.temperature = float(raw.get("temperature", self.temperature))
        tried = raw.get("tried_bins", {})
        if isinstance(tried, dict):
            self._tried_bins = {
                str(k): {int(v) for v in vals}
                for k, vals in tried.items()
                if isinstance(vals, list)
            }

    def _save_state(self) -> None:
        if self.state_path is None:
            return
        dump_state(
            self.state_path,
            {
                "best_reward": self.best_reward,
                "temperature": self.temperature,
                "pending_candidate": self.pending.log_payload() if self.pending else None,
                "tried_bins": {k: sorted(v) for k, v in self._tried_bins.items()},
            },
        )


def _first_non_noop_if_over_cap(candidate: ActionCandidate, max_steps_per_run: int) -> ActionCandidate:
    del max_steps_per_run
    return candidate


__all__ = ["HillClimbController"]
