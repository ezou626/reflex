from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np
from scipy.stats import norm
from skopt import Optimizer
from skopt.space import Integer, Real

from daemon_core.tuners import AppliedAction, TunerAction, TunerRegistry
from daemon_core.tuners.sysctl_util import read_sysctl, sysctl_name_to_path
from daemon_core.types import AggregatorSample, ControllerRunContext
from implementations.controllers.heuristic import HeuristicController
from implementations.controllers.reward import (
    N_FEATURES,
    compute_reward,
    extract_state_vec,
)
from implementations.executors import TunerActionExecutor


def _expected_improvement(
    mean: np.ndarray,
    std: np.ndarray,
    best_y: float,
    xi: float = 0.01,
) -> np.ndarray:
    improvement = mean - best_y - xi
    z = improvement / (std + 1e-9)
    return improvement * norm.cdf(z) + std * norm.pdf(z)


class _TunerBO:
    MIN_FIT_SAMPLES = 5

    def __init__(
        self,
        tuner_id: str,
        min_val: int,
        max_val: int,
        step: int,
        xi: float = 0.01,
    ) -> None:
        self.tuner_id = tuner_id
        self.min_val = min_val
        self.max_val = max_val
        self.step = max(1, step)
        self.xi = xi
        space = [Real(0.0, 1.0) for _ in range(N_FEATURES)] + [Integer(min_val, max_val)]
        self._opt = Optimizer(
            dimensions=space,
            base_estimator="GP",
            acq_func="EI",
            acq_optimizer="sampling",
            n_initial_points=self.MIN_FIT_SAMPLES,
            random_state=42,
        )
        self._best_y: float = -np.inf
        self._n_tells = 0

    def _candidates(self, current: int) -> list[int]:
        cands: set[int] = set()
        for mult in (1, 2):
            for sign in (-1, 1):
                v = current + sign * mult * self.step
                if self.min_val <= v <= self.max_val:
                    cands.add(v)
        return sorted(cands)

    def propose(self, state_vec: np.ndarray, current: int) -> int | None:
        if self._n_tells < self.MIN_FIT_SAMPLES or not self._opt.models:
            return None
        candidates = self._candidates(current)
        if not candidates:
            return None
        gp = self._opt.models[-1]
        x_cands = [list(state_vec) + [v] for v in candidates]
        x_transformed = self._opt.space.transform(x_cands)
        mean_neg, std = gp.predict(x_transformed, return_std=True)
        mean = -mean_neg
        scores = _expected_improvement(mean, std, self._best_y, xi=self.xi)
        return candidates[int(np.argmax(scores))]

    def update(self, state_vec: np.ndarray, value: int, reward: float) -> None:
        self._opt.tell(list(state_vec) + [value], -reward)
        self._best_y = max(self._best_y, reward)
        self._n_tells += 1

    @property
    def n_observations(self) -> int:
        return self._n_tells


@dataclass
class _PendingObs:
    window_count: int
    tuner_id: str
    value_applied: int
    state_vec: np.ndarray
    summary_before: dict[str, Any]


class BOController(HeuristicController):
    def __init__(
        self,
        registry: TunerRegistry,
        *,
        evaluate_after_windows: int = 3,
        xi: float = 0.01,
        per_tuner_cooldown_windows: int = 15,
        fallback: HeuristicController | None = None,
        max_history: int = 60,
    ) -> None:
        super().__init__(registry, max_history=max_history)
        self._evaluate_after = evaluate_after_windows
        self._xi = xi
        self._per_tuner_cooldown = per_tuner_cooldown_windows
        self._fallback = fallback
        self._surrogates: dict[str, _TunerBO] = {}
        self._pending: list[_PendingObs] = []
        self._window_count = 0
        self._tuner_cooldown_until: dict[str, int] = {}

    def _get_surrogate(self, tuner_id: str) -> _TunerBO | None:
        if tuner_id in self._surrogates:
            return self._surrogates[tuner_id]
        tuner = self.registry.get(tuner_id)
        if tuner is None:
            return None
        entry = getattr(tuner, "_entry", None)
        if entry is None or entry.min_value is None or entry.max_value is None:
            return None
        surrogate = _TunerBO(
            tuner_id=tuner_id,
            min_val=int(entry.min_value),
            max_val=int(entry.max_value),
            step=int(entry.step),
            xi=self._xi,
        )
        self._surrogates[tuner_id] = surrogate
        return surrogate

    async def accept_data(self, sample: AggregatorSample) -> None:
        await super().accept_data(sample)
        if self.history:
            self._window_count += 1
            current = self.history[-1]
            still_pending: list[_PendingObs] = []
            for obs in self._pending:
                if self._window_count >= obs.window_count + self._evaluate_after:
                    surrogate = self._surrogates.get(obs.tuner_id)
                    if surrogate is not None:
                        surrogate.update(
                            obs.state_vec,
                            obs.value_applied,
                            compute_reward(obs.summary_before, current),
                        )
                else:
                    still_pending.append(obs)
            self._pending = still_pending

    def propose(self, summary: dict[str, Any]) -> list[TunerAction]:
        actions: list[TunerAction] = []
        bo_covered: set[str] = set()
        state_vec = extract_state_vec(summary)

        for tuner_id in sorted(self.registry.catalog_entry_ids()):
            if not self.registry.is_enabled(tuner_id):
                continue
            if self._window_count < self._tuner_cooldown_until.get(tuner_id, 0):
                continue
            surrogate = self._get_surrogate(tuner_id)
            if surrogate is None or surrogate.n_observations < _TunerBO.MIN_FIT_SAMPLES:
                continue
            tuner = self.registry.get(tuner_id)
            if tuner is None or not tuner.supports(summary):
                continue
            entry = getattr(tuner, "_entry", None)
            if entry is None:
                continue
            try:
                current = int(read_sysctl(sysctl_name_to_path(entry.sysctl), entry.kind))
            except OSError:
                continue
            proposed = surrogate.propose(state_vec, current)
            if proposed is None or proposed == current:
                continue
            direction = "increase" if proposed > current else "decrease"
            actions.append(TunerAction(
                tuner_id=tuner_id,
                action_id=f"bo_{direction}",
                target=entry.sysctl,
                value=proposed,
                reason=f"GP-BO EI acquisition (n_obs={surrogate.n_observations})",
                priority=60,
                metadata={
                    "current": current,
                    "n_observations": surrogate.n_observations,
                    "xi": self._xi,
                },
            ))
            bo_covered.add(tuner_id)

        fallback = self._fallback or self
        for action in HeuristicController.propose(fallback, summary):
            if action.tuner_id in bo_covered:
                continue
            if self._window_count < self._tuner_cooldown_until.get(action.tuner_id, 0):
                continue
            actions.append(action)
        return actions

    def _record_applied(self, summary: dict[str, Any], applied: AppliedAction) -> None:
        surrogate = self._surrogates.get(applied.action.tuner_id)
        if surrogate is None:
            return
        self._pending.append(_PendingObs(
            window_count=self._window_count,
            tuner_id=applied.action.tuner_id,
            value_applied=int(applied.action.value),
            state_vec=extract_state_vec(summary),
            summary_before=summary,
        ))

    async def run(self, ctx: ControllerRunContext) -> None:
        if not self.history:
            await ctx.log_decision("bo", "no summaries available", {})
            return
        summary = self.history[-1]
        actions = self.propose(summary)
        await ctx.log_decision(
            "bo",
            "BO proposal pass complete",
            {"actions": len(actions), "trigger_reason": ctx.trigger.reason},
        )
        for action in sorted(actions, key=lambda a: a.priority, reverse=True):
            await ctx.enqueue_executor(
                TunerActionExecutor(
                    self.registry,
                    action,
                    on_applied=lambda applied, s=summary: self._record_applied(s, applied),
                ),
                {
                    "controller": "bo",
                    "tuner_id": action.tuner_id,
                    "action_id": action.action_id,
                    "priority": action.priority,
                },
            )
