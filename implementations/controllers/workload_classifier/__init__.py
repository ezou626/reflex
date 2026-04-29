from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np

from daemon_core.tuners import TunerAction, TunerRegistry
from daemon_core.types import AggregatorSample, ControllerRunContext
from implementations.executors import BatchTunerExecutor

DEFAULT_LIBRARY_PATH = Path(__file__).resolve().parent / "models" / "library.json"

# Feature space is now defined per library entry (`feature_keys` + `feature_norms`)
# rather than hardcoded — kmeans.py writes both alongside each centroid, so the
# classifier reads whatever space the library was trained in.
#
# Live summary dicts are produced by an aggregator wrapping src/loader2.c, with
# keys matching what scripts/tune_experiment2.py records:
#   p95_latency, throughput, mem, io, cpu, failures,
#   blk_p95_latency, ctx_switch_rate, direct_reclaim_rate, fork_rate


def _summary_get(summary: dict[str, Any], key: str) -> float:
    """Look up a metric from either nested sub-dict of an aggregator summary."""
    metrics = summary.get("metrics", {})
    host = summary.get("host_features", {})
    value = metrics.get(key)
    if value is None:
        value = host.get(key, 0.0)
    return float(value) if value is not None else 0.0


def _summary_to_vec(
    summary: dict[str, Any],
    feature_keys: list[str],
    feature_norms: list[float],
) -> np.ndarray:
    """Project a summary onto the same normalized feature space as the library entry."""
    return np.array(
        [min(_summary_get(summary, k) / n, 1.0) for k, n in zip(feature_keys, feature_norms)],
        dtype=np.float64,
    )


def _summary_from_sample(sample: AggregatorSample) -> dict[str, Any] | None:
    if isinstance(sample.sample, dict):
        return sample.sample
    return None


class WorkloadClassifier:
    """
    Nearest-centroid classifier over the eBPF/proc feature space.

    The classifier reads a library produced by scripts/tune_experiment.py. Each
    class entry carries a centroid and a pre-validated best tuner config.
    """

    def __init__(self, library_path: Path, max_distance: float = 0.35) -> None:
        self.library_path = library_path
        self._max_distance = max_distance
        self._entries: dict[str, dict[str, Any]] = {}

        if library_path.is_file():
            try:
                raw = json.loads(library_path.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    self._entries = raw
            except (OSError, json.JSONDecodeError, TypeError, ValueError):
                self._entries = {}

    @classmethod
    def from_model_dir(
        cls, model_dir: Path, max_distance: float = 0.35
    ) -> WorkloadClassifier:
        return cls(model_dir / "library.json", max_distance=max_distance)

    @classmethod
    def from_default_library(cls, max_distance: float = 0.35) -> WorkloadClassifier:
        return cls(DEFAULT_LIBRARY_PATH, max_distance=max_distance)

    def is_loaded(self) -> bool:
        return bool(self._entries)

    def known_classes(self) -> list[str]:
        return list(self._entries.keys())

    def classify(self, summary: dict[str, Any]) -> str | None:
        if not self._entries:
            return None
        best_name: str | None = None
        best_dist: float = self._max_distance
        for name, entry in self._entries.items():
            keys     = entry.get("feature_keys")
            norms    = entry.get("feature_norms")
            centroid = entry.get("centroid")
            if not keys or not norms or centroid is None:
                continue
            vec      = _summary_to_vec(summary, keys, norms)
            centroid_arr = np.array(centroid, dtype=np.float64)
            dist     = float(np.linalg.norm(vec - centroid_arr))
            if dist < best_dist:
                best_dist = dist
                best_name = name
        return best_name

    def best_config(self, workload_class: str) -> dict[str, int] | None:
        entry = self._entries.get(workload_class)
        if not entry:
            return None
        best_config = entry.get("best_config")
        if not best_config:
            return None
        return {key: int(value) for key, value in best_config.items()}

    def best_reward(self, workload_class: str) -> float | None:
        # kmeans.py writes `observed_best_reward`; v1 libraries used `best_reward`.
        entry = self._entries.get(workload_class)
        if not entry:
            return None
        val = entry.get("observed_best_reward", entry.get("best_reward"))
        return float(val) if val is not None else None


class WorkloadClassifierController:
    """
    Applies a pre-trained config when the detected workload class changes.

    Hysteresis via min_consecutive prevents churn when metrics sit near a class
    boundary. The class must be stable for min_consecutive samples before the
    controller schedules executor work.
    """

    def __init__(
        self,
        registry: TunerRegistry,
        classifier: WorkloadClassifier,
        *,
        min_consecutive: int = 3,
        max_history: int = 60,
    ) -> None:
        self.registry = registry
        self.classifier = classifier
        self.min_consecutive = min_consecutive
        self.max_history = max_history
        self.history: list[dict[str, Any]] = []
        self._confirmed_class: str | None = None
        self._candidate: str | None = None
        self._run = 0

    async def accept_data(self, sample: AggregatorSample) -> None:
        summary = _summary_from_sample(sample)
        if summary is None:
            return
        self.history.append(summary)
        self.history = self.history[-self.max_history :]

    def propose(self, summary: dict[str, Any]) -> list[TunerAction]:
        detected = self.classifier.classify(summary)

        if detected == self._candidate:
            self._run += 1
        else:
            self._candidate = detected
            self._run = 1

        if self._run < self.min_consecutive:
            return []

        if detected == self._confirmed_class:
            return []

        self._confirmed_class = detected

        if detected is None:
            return []

        best = self.classifier.best_config(detected)
        if not best:
            return []

        actions: list[TunerAction] = []
        for tuner_id, value in best.items():
            tuner = self.registry.get(tuner_id)
            if tuner is None or not tuner.supports(summary):
                continue
            entry = getattr(tuner, "_entry", None)
            if entry is None:
                continue
            actions.append(TunerAction(
                tuner_id=tuner_id,
                action_id="workload_class_config",
                target=entry.sysctl,
                value=value,
                reason=f"pre-trained config for workload_class={detected}",
                priority=75,
                metadata={"workload_class": detected},
            ))
        return actions

    async def run(self, ctx: ControllerRunContext) -> None:
        if not self.history:
            await ctx.log_decision("workload_classifier", "no summaries available", {})
            return
        summary = self.history[-1]
        actions = self.propose(summary)
        detected = self._candidate
        await ctx.log_decision(
            "workload_classifier",
            "workload classifier proposal pass complete",
            {
                "actions": len(actions),
                "candidate_class": detected,
                "confirmed_class": self._confirmed_class,
                "stable_windows": self._run,
                "trigger_reason": ctx.trigger.reason,
            },
        )
        if actions:
            await ctx.enqueue_executor(
                BatchTunerExecutor(self.registry, actions),
                {
                    "controller": "workload_classifier",
                    "action_count": len(actions),
                    "priority": max(action.priority for action in actions),
                    "tuner_ids": [action.tuner_id for action in actions],
                    "workload_class": actions[0].metadata.get("workload_class"),
                },
            )


__all__ = [
    "DEFAULT_LIBRARY_PATH",
    "WorkloadClassifier",
    "WorkloadClassifierController",
]
