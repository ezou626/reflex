from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np

from control.proposal_controller import ProposalController
from tuners.base import TunerAction
from tuners.registry import TunerRegistry


# 8-dim normalized feature space for centroid comparison.
# Keys may live in summary["metrics"] or summary["host_features"].
# Must stay in sync with _FEATURE_MAP in scripts/tune_experiment.py.
_FEATURE_MAP: list[tuple[str, float]] = [
    ("rq_latency_p95_us",           10_000.0),
    ("context_switch_rate_per_sec", 100_000.0),
    ("syscall_error_rate",          1.0),
    ("host_cpu_busy_ratio",         1.0),
    ("host_mem_available_ratio",    1.0),
    ("host_dirty_kb",               200_000.0),
    ("direct_reclaim_rate_per_sec", 100.0),
    ("blk_latency_p95_us",          50_000.0),
]


def _summary_to_vec(summary: dict[str, Any]) -> np.ndarray:
    metrics = summary.get("metrics", {})
    host = summary.get("host_features", {})

    def _get(k: str) -> float:
        v = metrics.get(k)
        if v is None:
            v = host.get(k, 0.0)
        return float(v) if v is not None else 0.0

    return np.array(
        [min(_get(k) / norm, 1.0) for k, norm in _FEATURE_MAP],
        dtype=np.float64,
    )


class WorkloadClassifier:
    """
    Nearest-centroid classifier over the 8-dim eBPF+proc feature space.

    Each workload class has a centroid learned from controlled experiments
    (tune_experiment.py) and a pre-validated best config. At runtime, the
    current summary is compared against all known centroids; the nearest
    class within max_distance is returned.

    Loads from models/library.json, which is written by tune_experiment.py.
    Returns None if the library is empty or no class is within threshold.
    """

    def __init__(self, library_path: Path, max_distance: float = 0.35) -> None:
        self._max_distance = max_distance
        self._entries: dict[str, dict[str, Any]] = {}

        if library_path.is_file():
            try:
                raw = json.loads(library_path.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    self._entries = raw
            except Exception:
                pass

    @classmethod
    def from_model_dir(
        cls, model_dir: Path, max_distance: float = 0.35
    ) -> WorkloadClassifier:
        return cls(model_dir / "library.json", max_distance=max_distance)

    def is_loaded(self) -> bool:
        return bool(self._entries)

    def known_classes(self) -> list[str]:
        return list(self._entries.keys())

    def classify(self, summary: dict[str, Any]) -> str | None:
        if not self._entries:
            return None
        vec = _summary_to_vec(summary)
        best_name: str | None = None
        best_dist: float = self._max_distance
        for name, entry in self._entries.items():
            centroid = np.array(entry["centroid"], dtype=np.float64)
            dist = float(np.linalg.norm(vec - centroid))
            if dist < best_dist:
                best_dist = dist
                best_name = name
        return best_name

    def best_config(self, workload_class: str) -> dict[str, int] | None:
        entry = self._entries.get(workload_class)
        if not entry:
            return None
        bc = entry.get("best_config")
        if not bc:
            return None
        return {k: int(v) for k, v in bc.items()}

    def best_reward(self, workload_class: str) -> float | None:
        entry = self._entries.get(workload_class)
        return float(entry["best_reward"]) if entry else None


class WorkloadAwareController(ProposalController):
    """
    Proposal controller that emits the pre-trained optimal config whenever
    the detected workload class changes.

    Sits at priority 75 — above BO (60) and heuristics (50) — so the
    decision engine picks it first. After class config is applied, BO
    continues to fine-tune from that starting point.

    Hysteresis via min_consecutive prevents thrashing when metrics sit near
    a class boundary. The class must be stable for min_consecutive windows
    before a switch is triggered.
    """

    def __init__(
        self,
        classifier: WorkloadClassifier,
        min_consecutive: int = 3,
    ) -> None:
        self._classifier = classifier
        self._min_consecutive = min_consecutive
        self._confirmed_class: str | None = None
        self._candidate: str | None = None
        self._run: int = 0

    def propose(
        self,
        summary: dict[str, Any],
        history: list[dict[str, Any]],
        *,
        registry: TunerRegistry,
    ) -> list[TunerAction]:
        del history

        detected = self._classifier.classify(summary)

        if detected == self._candidate:
            self._run += 1
        else:
            self._candidate = detected
            self._run = 1

        if self._run < self._min_consecutive:
            return []

        if detected == self._confirmed_class:
            return []

        self._confirmed_class = detected

        if detected is None:
            return []

        best = self._classifier.best_config(detected)
        if not best:
            return []

        actions: list[TunerAction] = []
        for tuner_id, value in best.items():
            tuner = registry.get(tuner_id)
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
