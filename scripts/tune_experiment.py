#!/usr/bin/env python3
"""
Offline controlled-experiment kernel tuner with unsupervised workload discovery.

Each experiment: apply a full config vector, fire a stressor, measure /proc
metrics for a fixed window, tell the GP, propose next config via EI.

Workload classes are discovered automatically via k-means over the collected
feature vectors — no labeling required. Run with different stressors across
sessions; the system figures out which programs produce similar system signatures
and assigns each group an optimal config.

All experiments are appended to models/experiments.jsonl across sessions.
After each run, k-means clusters all collected feature vectors and writes
models/library.json — the file WorkloadClassifier reads at runtime.

Usage (must run as root):
    sudo .venv/bin/python3 scripts/tune_experiment.py \\
        --stressor "./build/tester_mem 80 5" --experiments 30

    # Second session with a different program accumulates into the same dataset:
    sudo .venv/bin/python3 scripts/tune_experiment.py \\
        --stressor "stress-ng --vm 2 --vm-bytes 75%" --experiments 20

    # Dry-run (no sysctl writes, no stressor launched):
    sudo .venv/bin/python3 scripts/tune_experiment.py \\
        --stressor "./build/tester_mem 80 5" --experiments 5 --dry-run
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shlex
import signal
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "daemon"))

import joblib
import numpy as np
from sklearn.cluster import KMeans
from sklearn.metrics import silhouette_score
from skopt import Optimizer
from skopt.space import Integer

from config.loaders import load_tuner_catalog
from tuners.sysctl_util import read_sysctl, write_sysctl, sysctl_name_to_path

REPO_ROOT = Path(__file__).resolve().parent.parent

JOINT_TUNER_IDS = [
    "sysctl_vm_swappiness",
    "sysctl_vm_dirty_ratio",
    "sysctl_vm_vfs_cache_pressure",
]

# Feature space for clustering (must match workload_classifier.py _FEATURE_MAP).
# Each entry: (host_features key, normalization denominator)
_FEATURE_MAP = [
    ("cpu_busy",             1.0),
    ("mem_available_ratio",  1.0),
    ("dirty_kb",             200_000.0),
    ("loadavg",              10.0),
]


# ---------------------------------------------------------------------------
# /proc metric helpers (no eBPF required)
# ---------------------------------------------------------------------------

def _proc_stat() -> tuple[int, int] | None:
    try:
        for line in Path("/proc/stat").read_text().splitlines():
            if line.startswith("cpu "):
                parts = [int(x) for x in line.split()[1:]]
                return sum(parts), parts[3]
    except OSError:
        pass
    return None


def _meminfo() -> dict[str, int]:
    want = {"MemTotal", "MemAvailable", "Dirty"}
    out: dict[str, int] = {}
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if ":" not in line:
                continue
            k, rest = line.split(":", 1)
            if k in want:
                out[k] = int(rest.strip().split()[0])
    except OSError:
        pass
    return out


def _loadavg() -> float:
    try:
        return float(Path("/proc/loadavg").read_text().split()[0])
    except OSError:
        return 0.0


def measure_window(duration_s: float, sample_hz: float = 2.0) -> dict[str, float]:
    """Poll /proc at sample_hz for duration_s, return time-averaged metrics."""
    interval = 1.0 / sample_hz
    deadline = time.monotonic() + duration_s
    prev_stat = _proc_stat()
    buckets: dict[str, list[float]] = {
        "cpu_busy": [], "mem_available_ratio": [], "dirty_kb": [], "loadavg": []
    }

    while True:
        sleep_for = min(interval, max(0.0, deadline - time.monotonic()))
        if sleep_for <= 0:
            break
        time.sleep(sleep_for)

        now_stat = _proc_stat()
        mem = _meminfo()

        if now_stat and prev_stat:
            d_total = max(now_stat[0] - prev_stat[0], 1)
            d_idle  = max(now_stat[1] - prev_stat[1], 0)
            buckets["cpu_busy"].append(max(0.0, min(1.0, 1.0 - d_idle / d_total)))
        prev_stat = now_stat

        mem_total = mem.get("MemTotal", 0)
        if mem_total > 0:
            buckets["mem_available_ratio"].append(mem.get("MemAvailable", 0) / mem_total)
        buckets["dirty_kb"].append(float(mem.get("Dirty", 0)))
        buckets["loadavg"].append(_loadavg())

    return {k: float(np.mean(v)) if v else 0.0 for k, v in buckets.items()}


def metrics_to_feature_vec(metrics: dict[str, float]) -> list[float]:
    return [min(metrics.get(k, 0.0) / norm, 1.0) for k, norm in _FEATURE_MAP]


def compute_absolute_reward(metrics: dict[str, float]) -> float:
    """Scalar reward for one measurement window. Higher is better."""
    return (
        0.45 * metrics.get("mem_available_ratio", 0.0)
        - 0.25 * metrics.get("cpu_busy", 1.0)
        - 0.20 * min(metrics.get("dirty_kb", 0.0) / 200_000.0, 1.0)
        - 0.10 * min(metrics.get("loadavg", 0.0) / 10.0, 1.0)
    )


# ---------------------------------------------------------------------------
# Clustering
# ---------------------------------------------------------------------------

def _fit_cluster_gp(
    cluster_exps: list[dict],
    space: list,
) -> list[int]:
    """
    Fit a GP on all (config, reward) pairs from one cluster and return the
    config at the predicted maximum.  This generalises beyond the observed
    points so the deployed config can be better than any single experiment.

    If the cluster has fewer points than MIN_FIT_SAMPLES we fall back to
    argmax over the raw observations (GP would overfit anyway).
    """
    MIN_FIT = 5
    configs  = [list(e["config"].values()) for e in cluster_exps]
    rewards  = [e["reward"] for e in cluster_exps]

    if len(cluster_exps) < MIN_FIT:
        best_i = int(np.argmax(rewards))
        return configs[best_i]

    opt = Optimizer(
        dimensions=space,
        base_estimator="GP",
        acq_func="EI",
        n_initial_points=0,   # skip random phase; we supply all data via tell()
        random_state=42,
    )
    for cfg, r in zip(configs, rewards):
        opt.tell(cfg, -r)     # skopt minimises → negate

    # Predict over a dense grid of candidate configs and return the argmax.
    # Using opt.ask() here would give the next *exploration* point, not the
    # predicted optimum.  We want exploitation: the config with highest μ.
    gp = opt.models[-1]
    dims = [list(range(int(d.low), int(d.high) + 1, max(1, (int(d.high) - int(d.low)) // 20)))
            for d in opt.space.dimensions]
    grid = [[a, b, c] for a in dims[0] for b in dims[1] for c in dims[2]]
    X_t  = opt.space.transform(grid)
    mean_neg, _ = gp.predict(X_t, return_std=True)
    best_i = int(np.argmin(mean_neg))   # minimising negated reward
    return grid[best_i]


def cluster_and_save_library(
    experiments_path: Path,
    library_path: Path,
    catalog_path: Path | None = None,
    max_k: int = 6,
) -> None:
    """
    1. Load all collected experiments.
    2. Auto-select k via silhouette score and fit k-means on feature vectors.
    3. For each cluster, fit a per-cluster GP on (config, reward) pairs and
       find the predicted optimal config via GP mean prediction.
    4. Write library.json — the file WorkloadClassifier reads at runtime.

    This is the offline BO refinement step: the GP generalises beyond
    observed points so the deployed config can exceed any single experiment.
    """
    if not experiments_path.is_file():
        return

    experiments: list[dict] = []
    for line in experiments_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            experiments.append(json.loads(line))
        except json.JSONDecodeError:
            continue

    if len(experiments) < 2:
        print(f"Not enough experiments to cluster ({len(experiments)} collected).")
        return

    X = np.array([e["feature_vec"] for e in experiments], dtype=np.float64)

    # Auto-select k: try 2..cap, pick highest silhouette.
    cap = min(max_k, len(experiments) - 1)
    best_k, best_score = 1, -1.0
    for k in range(2, cap + 1):
        km = KMeans(n_clusters=k, random_state=42, n_init=10)
        labels = km.fit_predict(X)
        if len(set(labels)) < 2:
            continue
        score = float(silhouette_score(X, labels))
        if score > best_score:
            best_k, best_score = k, score

    km = KMeans(n_clusters=best_k, random_state=42, n_init=10)
    labels = km.fit_predict(X).tolist()

    # Rebuild the config space from the first experiment's config keys.
    # Assumes all experiments share the same tuner set.
    sample_config = experiments[0]["config"]
    tuner_ids = list(sample_config.keys())

    print(f"\nClustering: {len(experiments)} experiments → {best_k} cluster(s) "
          f"(silhouette={best_score:.3f})")

    library: dict[str, Any] = {}
    for c in range(best_k):
        indices      = [i for i, l in enumerate(labels) if l == c]
        cluster_exps = [experiments[i] for i in indices]
        centroid     = km.cluster_centers_[c].tolist()
        stressors    = [e.get("stressor", "") for e in cluster_exps]
        dominant     = Counter(stressors).most_common(1)[0][0]

        # Per-cluster GP: use full catalog bounds so the GP can propose configs
        # beyond the observed range.  Fall back to observed min/max if no catalog.
        if catalog_path is not None:
            cat = load_tuner_catalog(catalog_path)
            by_id = {e.id: e for e in cat.tuners}
            space = [
                Integer(
                    int(by_id[tid].min_value) if tid in by_id and by_id[tid].min_value is not None else min(e["config"][tid] for e in cluster_exps),
                    int(by_id[tid].max_value) if tid in by_id and by_id[tid].max_value is not None else max(e["config"][tid] for e in cluster_exps),
                    name=tid,
                )
                for tid in tuner_ids
            ]
        else:
            all_configs = [list(e["config"].values()) for e in cluster_exps]
            col_min = [min(cfg[j] for cfg in all_configs) for j in range(len(tuner_ids))]
            col_max = [max(cfg[j] for cfg in all_configs) for j in range(len(tuner_ids))]
            space   = [Integer(lo, hi, name=tid)
                       for tid, lo, hi in zip(tuner_ids, col_min, col_max)]

        best_config_list = _fit_cluster_gp(cluster_exps, space)
        best_config      = dict(zip(tuner_ids, [int(v) for v in best_config_list]))

        # Report observed best alongside GP prediction for transparency.
        obs_best = max(cluster_exps, key=lambda e: e["reward"])

        library[f"cluster_{c}"] = {
            "centroid": centroid,
            "best_config": best_config,
            "best_reward": round(obs_best["reward"], 6),
            "n_observations": len(cluster_exps),
            "dominant_stressor": dominant,
        }

        print(f"  cluster_{c}: n={len(cluster_exps)}  "
              f"stressor={dominant}  "
              f"obs_best={obs_best['reward']:+.4f}  "
              f"gp_config={best_config}")

    library_path.parent.mkdir(parents=True, exist_ok=True)
    library_path.write_text(json.dumps(library, indent=2), encoding="utf-8")
    print(f"Library saved: {library_path}")


# ---------------------------------------------------------------------------
# Experiment runner
# ---------------------------------------------------------------------------

def _stressor_key(cmd: list[str]) -> str:
    """Short stable key for a stressor command, used to name its GP model file."""
    basename = Path(cmd[0]).stem if cmd else "unknown"
    digest = hashlib.md5(" ".join(cmd).encode()).hexdigest()[:6]
    return f"{basename}_{digest}"


class ExperimentRunner:
    """
    Joint-space Bayesian Optimization for one stressor command.
    GP model is persisted per stressor (keyed by command hash) so separate
    sessions accumulate observations toward the same surrogate.
    """

    def __init__(
        self,
        catalog_path: Path,
        model_dir: Path,
        stressor_cmd: list[str],
        duration_s: float,
        warmup_s: float,
        dry_run: bool,
    ) -> None:
        self.stressor_cmd  = stressor_cmd
        self.duration      = duration_s
        self.warmup        = warmup_s
        self.dry_run       = dry_run
        self._all_metrics: list[dict[str, float]] = []

        key = _stressor_key(stressor_cmd)
        self.model_path = model_dir / f"gp_{key}.pkl"

        catalog = load_tuner_catalog(catalog_path)
        by_id = {e.id: e for e in catalog.tuners}
        self.entries = [
            by_id[tid] for tid in JOINT_TUNER_IDS
            if tid in by_id
            and by_id[tid].min_value is not None
            and by_id[tid].max_value is not None
        ]
        if not self.entries:
            raise RuntimeError(
                "No tuners with min_value/max_value found in catalog."
            )

        self.baseline: dict[str, int] = {
            e.id: int(read_sysctl(sysctl_name_to_path(e.sysctl), e.kind))
            for e in self.entries
        }

        space = [
            Integer(int(e.min_value), int(e.max_value), name=e.id)
            for e in self.entries
        ]

        if self.model_path.exists():
            self.opt: Optimizer = joblib.load(self.model_path)
            print(f"Resumed GP model: {self.model_path} "
                  f"({len(self.opt.Xi)} prior observations)")
        else:
            self.opt = Optimizer(
                dimensions=space,
                base_estimator="GP",
                acq_func="EI",
                n_initial_points=6,
                random_state=42,
            )
            print(f"New GP model for stressor: {' '.join(stressor_cmd)}")

        self.best_reward: float = -np.inf
        self.best_config: list[int] | None = None
        if self.opt.Xi:
            rewards = [-y for y in self.opt.yi]
            i = int(np.argmax(rewards))
            self.best_reward = float(rewards[i])
            self.best_config = list(self.opt.Xi[i])

    def _apply(self, config: list[int]) -> None:
        if self.dry_run:
            return
        for e, val in zip(self.entries, config):
            write_sysctl(sysctl_name_to_path(e.sysctl), val, e.kind)

    def restore_baseline(self) -> None:
        if self.dry_run:
            return
        for e in self.entries:
            write_sysctl(sysctl_name_to_path(e.sysctl), self.baseline[e.id], e.kind)

    def _start_stressor(self) -> subprocess.Popen | None:
        if self.dry_run or not self.stressor_cmd:
            return None
        return subprocess.Popen(
            self.stressor_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )

    def _stop_stressor(self, proc: subprocess.Popen | None) -> None:
        if proc is None:
            return
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()

    def run_one(self) -> dict[str, Any]:
        config = self.opt.ask()
        label = {e.sysctl: int(v) for e, v in zip(self.entries, config)}
        print(f"  config={label} ...", end="", flush=True)

        self._apply(config)
        proc = self._start_stressor()
        if self.warmup > 0:
            time.sleep(self.warmup)

        metrics = measure_window(self.duration)
        self._stop_stressor(proc)
        self.restore_baseline()

        reward = compute_absolute_reward(metrics)
        self.opt.tell(config, -reward)
        self._all_metrics.append(metrics)

        if reward > self.best_reward:
            self.best_reward = reward
            self.best_config = [int(v) for v in config]

        _print_result(reward, metrics)
        return {
            "feature_vec": metrics_to_feature_vec(metrics),
            "config": {e.id: int(v) for e, v in zip(self.entries, config)},
            "reward": reward,
            "stressor": " ".join(self.stressor_cmd),
        }

    def save_gp(self) -> None:
        self.model_path.parent.mkdir(parents=True, exist_ok=True)
        joblib.dump(self.opt, self.model_path)

    def best_config_dict(self) -> dict[str, int] | None:
        if self.best_config is None:
            return None
        return {e.id: int(v) for e, v in zip(self.entries, self.best_config)}

    def n_observations(self) -> int:
        return len(self.opt.Xi)


# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------

def _print_result(reward: float, metrics: dict[str, float]) -> None:
    parts = [f"reward={reward:+.4f}"]
    if "mem_available_ratio" in metrics:
        parts.append(f"mem_avail={metrics['mem_available_ratio']:.2%}")
    if "cpu_busy" in metrics:
        parts.append(f"cpu={metrics['cpu_busy']:.2%}")
    if "dirty_kb" in metrics:
        parts.append(f"dirty={metrics['dirty_kb']:.0f}kb")
    print("  " + "  ".join(parts))


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Offline controlled-experiment kernel tuner with auto workload discovery"
    )
    parser.add_argument(
        "--stressor",
        type=str,
        required=True,
        metavar="CMD",
        help='Stressor command as a quoted string, e.g. --stressor "./build/tester_mem 80 5"',
    )
    parser.add_argument(
        "--experiments", type=int, default=20,
        help="Number of experiments to run (default: 20)",
    )
    parser.add_argument(
        "--duration", type=float, default=45.0,
        help="Measurement window per experiment in seconds (default: 45)",
    )
    parser.add_argument(
        "--warmup", type=float, default=10.0,
        help="Seconds to let stressor ramp before measuring (default: 10)",
    )
    parser.add_argument(
        "--settle", type=float, default=8.0,
        help="Seconds between experiments for memory to settle (default: 8)",
    )
    parser.add_argument(
        "--model-dir", type=Path, default=REPO_ROOT / "models",
        help="Directory for GP models and library (default: models/)",
    )
    parser.add_argument(
        "--catalog", type=Path,
        default=REPO_ROOT / "configs" / "tuner_catalog.yaml",
    )
    parser.add_argument(
        "--max-clusters", type=int, default=6,
        help="Maximum number of clusters to consider (default: 6)",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Skip sysctl writes and stressor; useful for testing the loop",
    )
    args = parser.parse_args()

    stressor_cmd = shlex.split(args.stressor)
    if not stressor_cmd:
        print("error: --stressor requires at least one argument", file=sys.stderr)
        return 1

    if os.geteuid() != 0 and not args.dry_run:
        print("error: must run as root to write sysctl values", file=sys.stderr)
        return 1

    binary = Path(stressor_cmd[0])
    if binary.exists() is False and not args.dry_run:
        print(f"error: stressor binary not found: {binary}", file=sys.stderr)
        return 1

    runner = ExperimentRunner(
        catalog_path=args.catalog,
        model_dir=args.model_dir,
        stressor_cmd=stressor_cmd,
        duration_s=args.duration,
        warmup_s=args.warmup,
        dry_run=args.dry_run,
    )

    experiments_path = args.model_dir / "experiments.jsonl"
    library_path     = args.model_dir / "library.json"

    print(f"\nStressor:  {' '.join(stressor_cmd)}")
    print(f"Baseline:  {runner.baseline}")
    print(f"Knobs:     {[e.sysctl for e in runner.entries]}")
    print(
        f"Plan:      {args.experiments} experiments × "
        f"({args.warmup}s warmup + {args.duration}s measure + {args.settle}s settle) "
        f"≈ {args.experiments * (args.warmup + args.duration + args.settle) / 60:.1f} min\n"
    )

    interrupted = False

    def _handle_stop(_sig: int, _frame: Any) -> None:
        nonlocal interrupted
        interrupted = True

    signal.signal(signal.SIGINT, _handle_stop)
    signal.signal(signal.SIGTERM, _handle_stop)

    args.model_dir.mkdir(parents=True, exist_ok=True)
    results_fh = experiments_path.open("a", encoding="utf-8", buffering=1)

    try:
        for i in range(args.experiments):
            if interrupted:
                print("\nInterrupted — restoring baseline and saving.")
                break

            print(f"[{i+1:3d}/{args.experiments}]", end="", flush=True)
            try:
                result = runner.run_one()
            except Exception as exc:
                print(f"  ERROR: {exc} — restoring baseline")
                runner.restore_baseline()
                continue

            results_fh.write(json.dumps(result, ensure_ascii=False) + "\n")
            runner.save_gp()

            if i < args.experiments - 1 and not interrupted:
                time.sleep(args.settle)
    finally:
        results_fh.close()

    runner.restore_baseline()
    runner.save_gp()

    best = runner.best_config_dict()
    print(f"\n{'='*60}")
    print(f"Best config this run  reward={runner.best_reward:+.4f}")
    for tid, val in (best or {}).items():
        print(f"  {tid}: {val}")
    print(f"GP model: {runner.model_path}  ({runner.n_observations()} observations)")

    cluster_and_save_library(
        experiments_path, library_path,
        catalog_path=args.catalog,
        max_k=args.max_clusters,
    )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
