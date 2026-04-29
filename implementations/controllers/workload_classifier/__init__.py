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


# Adapter map: library feature key (v2) → aggregator metric key (v1).
# The current CurrentPayloadAggregator emits v1 keys; the trained library uses
# v2 keys. This map lets the classifier read v1 summaries against v2 centroids
# without changing the aggregator. See kmeans.py NORMS for the full v2 list.
_KEY_ALIASES: dict[str, str] = {
    "p95_latency":         "rq_latency_p95_us",
    "blk_p95_latency":     "blk_latency_p95_us",
    "ctx_switch_rate":     "context_switch_rate_per_sec",
    "direct_reclaim_rate": "direct_reclaim_rate_per_sec",
    "failures":            "syscall_error_rate_per_sec",
}


def _psi_some_avg10(resource: str) -> float:
    """Parse /proc/pressure/<resource> → 'some avg10' value (0 if unavailable)."""
    try:
        for line in Path(f"/proc/pressure/{resource}").read_text().splitlines():
            if line.startswith("some "):
                for tok in line.split():
                    if tok.startswith("avg10="):
                        return float(tok.split("=", 1)[1])
    except OSError:
        pass
    return 0.0


def _summary_get(summary: dict[str, Any], key: str) -> float:
    """
    Resolve a v2 library feature key against a v1 aggregator summary.

    Order of resolution:
      1. PSI keys (mem/io/cpu) — read /proc/pressure/* directly.
      2. Derived rates (throughput, fork_rate) — compute from event_counts and window_sec.
      3. Aliased v1 keys — look up under their v1 name in metrics/host_features.
      4. Direct lookup — for keys that already match (forward-compat with a v2 aggregator).
    """
    if key in ("mem", "io", "cpu"):
        return _psi_some_avg10("memory" if key == "mem" else key)

    metrics    = summary.get("metrics", {})
    host       = summary.get("host_features", {})
    window_sec = max(float(summary.get("window_sec", 1.0)), 1e-6)
    event_cnt  = summary.get("event_counts", {})

    if key == "throughput":
        # No total syscall count in v1 summary; sum of top_syscalls is the closest proxy.
        return sum(int(s.get("count", 0)) for s in summary.get("top_syscalls", [])) / window_sec
    if key == "fork_rate":
        return float(event_cnt.get("fork", 0)) / window_sec

    lookup = _KEY_ALIASES.get(key, key)
    value  = metrics.get(lookup)
    if value is None:
        value = host.get(lookup, 0.0)
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
        return self.classify_verbose(summary)[0]

    def classify_verbose(
        self, summary: dict[str, Any]
    ) -> tuple[str | None, dict[str, float], dict[str, list[float]]]:
        """Returns (best_class, {class: distance}, {class: feature_vec})."""
        distances: dict[str, float] = {}
        vecs: dict[str, list[float]] = {}
        if not self._entries:
            return None, distances, vecs
        best_name: str | None = None
        best_dist: float = self._max_distance
        for name, entry in self._entries.items():
            keys     = entry.get("feature_keys")
            norms    = entry.get("feature_norms")
            centroid = entry.get("centroid")
            if not keys or not norms or centroid is None:
                continue
            vec          = _summary_to_vec(summary, keys, norms)
            centroid_arr = np.array(centroid, dtype=np.float64)
            dist         = float(np.linalg.norm(vec - centroid_arr))
            distances[name] = dist
            vecs[name] = list(vec)
            if dist < best_dist:
                best_dist = dist
                best_name = name
        return best_name, distances, vecs

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

        # Compute verbose distances for display (no state change)
        detected_raw, distances, vecs = self.classifier.classify_verbose(summary)

        # propose() updates hysteresis state and returns actions
        actions = self.propose(summary)

        # Print feature vec using first class's feature_keys as the key list
        first_class = next(iter(self.classifier._entries), None)
        if first_class:
            entry = self.classifier._entries[first_class]
            keys = entry.get("feature_keys", [])
            vec = vecs.get(first_class, [])
            feat_str = "  ".join(f"{k}={v:.3f}" for k, v in zip(keys, vec))
            print(f"[features]   {feat_str}", flush=True)

        # Print distance to every centroid
        threshold = self.classifier._max_distance
        for cls, dist in sorted(distances.items()):
            marker = " <-- SELECTED" if cls == detected_raw else ""
            within = "within" if dist <= threshold else "beyond"
            print(f"  {cls}: dist={dist:.4f} ({within} threshold={threshold:.2f}){marker}", flush=True)

        if detected_raw is None:
            print(
                f"[classifier] no class within threshold — holding confirmed={self._confirmed_class}",
                flush=True,
            )
        else:
            stability = f"{self._run}/{self.min_consecutive}"
            apply_str = "  *APPLY*" if actions else ""
            print(
                f"[classifier] candidate={detected_raw}  stable={stability}"
                f"  confirmed={self._confirmed_class}{apply_str}",
                flush=True,
            )
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
