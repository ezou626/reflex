from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np
from scipy.stats import norm
from skopt import Optimizer
from skopt.space import Integer, Real

from control.proposal_controller import ProposalController
from reward import compute_reward, extract_state_vec, N_FEATURES
from tuners.base import TunerAction
from tuners.registry import TunerRegistry
from tuners.sysctl_util import read_sysctl, sysctl_name_to_path


def _expected_improvement(
    mean: np.ndarray,
    std: np.ndarray,
    best_y: float,
    xi: float = 0.01,
) -> np.ndarray:
    """
    Expected Improvement acquisition function (maximization).

    EI(x) = (μ(x) - f* - ξ) · Φ(z) + σ(x) · φ(z)
    where z = (μ(x) - f* - ξ) / σ(x)

    xi is a small jitter that trades off exploitation vs exploration.
    """
    improvement = mean - best_y - xi
    z = improvement / (std + 1e-9)
    return improvement * norm.cdf(z) + std * norm.pdf(z)


class _TunerBO:
    """
    Per-tuner Bayesian Optimization surrogate using scikit-optimize's GP.

    Input space: [normalized_state_features..., knob_value]
    Acquisition: Expected Improvement over discrete candidate knob values.

    Uses skopt's Optimizer for GP fitting (Matern kernel, automatic
    hyperparameter optimization via marginal likelihood maximization).
    We call tell() directly with observed (x, -reward) pairs — skopt minimizes,
    so rewards are negated — then score candidates ourselves using EI.
    """

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
        self._n_tells: int = 0

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
        X_cands = [list(state_vec) + [v] for v in candidates]
        X_transformed = self._opt.space.transform(X_cands)

        # GP predicts in negated space (skopt minimizes); flip back for EI
        mean_neg, std = gp.predict(X_transformed, return_std=True)
        mean = -mean_neg

        ei_scores = _expected_improvement(mean, std, self._best_y, xi=self.xi)
        return candidates[int(np.argmax(ei_scores))]

    def update(self, state_vec: np.ndarray, value: int, reward: float) -> None:
        x = list(state_vec) + [value]
        self._opt.tell(x, -reward)  # skopt minimizes
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


class BOProposalController(ProposalController):
    """
    Bayesian Optimization proposal controller.

    One GP surrogate per tuner, updated online as observations mature.
    Falls back to the heuristic controller until each tuner has enough
    observations for the GP to be meaningful.

    Call record_applied() from the daemon after an action is confirmed applied
    so only real system responses train the GP.
    """

    def __init__(
        self,
        evaluate_after_windows: int = 3,
        xi: float = 0.01,
        per_tuner_cooldown_windows: int = 15,
        fallback: ProposalController | None = None,
    ) -> None:
        self._evaluate_after = evaluate_after_windows
        self._xi = xi
        self._per_tuner_cooldown = per_tuner_cooldown_windows
        self._fallback = fallback
        self._surrogates: dict[str, _TunerBO] = {}
        self._pending: list[_PendingObs] = []
        self._window_count = 0
        self._tuner_cooldown_until: dict[str, int] = {}  # tuner_id -> window_count

    def _get_surrogate(self, tuner_id: str, registry: TunerRegistry) -> _TunerBO | None:
        if tuner_id in self._surrogates:
            return self._surrogates[tuner_id]
        tuner = registry.get(tuner_id)
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

    def propose(
        self,
        summary: dict[str, Any],
        history: list[dict[str, Any]],
        *,
        registry: TunerRegistry,
    ) -> list[TunerAction]:
        self._window_count += 1

        # harvest matured pending observations and update GP surrogates
        still_pending: list[_PendingObs] = []
        for obs in self._pending:
            if self._window_count >= obs.window_count + self._evaluate_after:
                reward = compute_reward(obs.summary_before, summary)
                surrogate = self._surrogates.get(obs.tuner_id)
                if surrogate is not None:
                    surrogate.update(obs.state_vec, obs.value_applied, reward)
            else:
                still_pending.append(obs)
        self._pending = still_pending

        actions: list[TunerAction] = []
        bo_covered: set[str] = set()
        state_vec = extract_state_vec(summary)

        for tuner_id in sorted(registry.catalog_entry_ids()):
            if not registry.is_enabled(tuner_id):
                continue
            if self._window_count < self._tuner_cooldown_until.get(tuner_id, 0):
                continue
            surrogate = self._get_surrogate(tuner_id, registry)
            if surrogate is None or surrogate.n_observations < _TunerBO.MIN_FIT_SAMPLES:
                continue

            tuner = registry.get(tuner_id)
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

        # heuristic fallback for cold-start tuners not on per-tuner cooldown
        if self._fallback is not None:
            for a in self._fallback.propose(summary, history, registry=registry):
                if a.tuner_id not in bo_covered:
                    if self._window_count < self._tuner_cooldown_until.get(a.tuner_id, 0):
                        continue
                    actions.append(a)

        return actions

    def record_applied(
        self,
        tuner_id: str,
        value: int,
        summary: dict[str, Any],
    ) -> None:
        """Register a confirmed applied action for future GP update."""
        surrogate = self._surrogates.get(tuner_id)
        if surrogate is None:
            return
        self._pending.append(_PendingObs(
            window_count=self._window_count,
            tuner_id=tuner_id,
            value_applied=value,
            state_vec=extract_state_vec(summary),
            summary_before=summary,
        ))

    def record_rollback(self, tuner_id: str) -> None:
        """
        Cancel pending observations for a rolled-back tuner and apply a
        per-tuner cooldown so other tuners get exploration turns.

        Without this, the GP would train on outcomes measured after the
        rollback has already fired — i.e. the system returning to baseline —
        which gives completely confounded reward signal.
        """
        self._pending = [o for o in self._pending if o.tuner_id != tuner_id]
        self._tuner_cooldown_until[tuner_id] = self._window_count + self._per_tuner_cooldown
