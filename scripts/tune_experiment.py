#!/usr/bin/env python3
"""
Offline controlled-experiment kernel tuner with unsupervised workload discovery.

Each experiment: apply a full config vector, fire a stressor, run the eBPF
daemon for a measurement window, tell the GP, propose next config via EI.

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
import tempfile
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
    # memory reclaim
    "sysctl_vm_swappiness",
    "sysctl_vm_dirty_ratio",
    "sysctl_vm_dirty_background_ratio",
    "sysctl_vm_vfs_cache_pressure",
    "sysctl_vm_watermark_scale_factor",
    "sysctl_vm_min_free_kbytes",
    # cpu scheduler
    "sysctl_kernel_sched_cfs_bandwidth_slice_us",
    "sysctl_kernel_sched_autogroup_enabled",
]

# Feature space for clustering. Must stay in sync with
# _FEATURE_MAP in daemon/control/workload_classifier.py.
# Keys are looked up from daemon summary["metrics"] or summary["host_features"].
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


# ---------------------------------------------------------------------------
# Daemon integration
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Cgroup helpers — mirror what run.sh does for the always-on daemon
# ---------------------------------------------------------------------------

def _create_stressor_cgroup() -> tuple[Path | None, int | None]:
    """
    Create a dedicated cgroup for the stressor process.
    Returns (cgroup_dir, cgroup_id) or (None, None) on failure.
    The cgroup ID (inode number of the cgroup dir) is what the eBPF
    whitelist uses to filter events to only this workload.
    """
    cgroup_dir = Path(f"/sys/fs/cgroup/reflex_tune_{os.getpid()}")
    try:
        cgroup_dir.mkdir(exist_ok=True)
        cgid = cgroup_dir.stat().st_ino
        return cgroup_dir, cgid
    except OSError as e:
        print(f"  [warn] cgroup creation failed ({e}) — eBPF will track all processes")
        return None, None


def _move_to_cgroup(pid: int, cgroup_dir: Path) -> None:
    try:
        (cgroup_dir / "cgroup.procs").write_text(str(pid), encoding="utf-8")
    except OSError as e:
        print(f"  [warn] could not move stressor to cgroup: {e}")


def _cleanup_cgroup(cgroup_dir: Path | None) -> None:
    if cgroup_dir is None or not cgroup_dir.exists():
        return
    try:
        cgroup_dir.rmdir()
    except OSError:
        pass


class DaemonContext:
    """
    Manages the eBPF daemon subprocess for one measurement window.

    Starts the daemon with a fresh summary output file and the stressor's
    cgroup ID so the eBPF whitelist only tracks that workload — mirroring
    exactly what run.sh does for the always-on daemon.
    """

    def __init__(
        self,
        summary_path: Path,
        dry_run: bool = False,
        cgroup_id: int | None = None,
    ) -> None:
        self.summary_path = summary_path
        self.dry_run = dry_run
        self.cgroup_id = cgroup_id
        self._proc: subprocess.Popen | None = None

    def start(self) -> None:
        if self.dry_run:
            return
        daemon_main = REPO_ROOT / "daemon" / "main.py"
        cmd = [
            sys.executable, str(daemon_main),
            "--summary-output", str(self.summary_path),
            "--dry-run",          # daemon must not apply sysctl changes
            "--window-sec", "2",
            "--proc-sample-sec", "2",
        ]
        if self.cgroup_id is not None:
            cmd += ["--cgroup-ids", str(self.cgroup_id)]
        self._proc = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            cwd=str(REPO_ROOT),
        )
        # Let the BPF loader attach all probes and populate the cgroup
        # whitelist before the stressor starts generating events.
        time.sleep(4)

    def stop(self) -> None:
        if self._proc is None:
            return
        self._proc.terminate()
        try:
            self._proc.wait(timeout=8)
        except subprocess.TimeoutExpired:
            self._proc.kill()
            self._proc.wait()
        self._proc = None

    def __enter__(self) -> DaemonContext:
        self.start()
        return self

    def __exit__(self, *_: Any) -> None:
        self.stop()


def _read_avg_summary(summary_path: Path, after_ts: float) -> dict[str, Any]:
    """
    Read daemon window summaries from summary_path, keeping only windows
    whose end timestamp is >= after_ts (i.e. within the measurement window).
    Returns a single merged dict with averaged "metrics" and "host_features".
    """
    summaries: list[dict] = []
    if summary_path.is_file():
        for line in summary_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if obj.get("record_type") != "window_summary":
                continue
            if float(obj.get("window_end_unix_s", 0)) >= after_ts:
                summaries.append(obj)

    if not summaries:
        return {"metrics": {}, "host_features": {}}

    # Collect numeric keys from each sub-dict and average across windows.
    metric_keys: set[str] = set()
    host_keys: set[str] = set()
    for s in summaries:
        metric_keys.update(
            k for k, v in s.get("metrics", {}).items() if isinstance(v, (int, float))
        )
        host_keys.update(
            k for k, v in s.get("host_features", {}).items() if isinstance(v, (int, float))
        )

    def _avg(vals: list) -> float:
        clean = [float(v) for v in vals if isinstance(v, (int, float))]
        return float(np.mean(clean)) if clean else 0.0

    avg_metrics = {
        k: _avg([s.get("metrics", {}).get(k) for s in summaries])
        for k in metric_keys
    }
    avg_host = {
        k: _avg([s.get("host_features", {}).get(k) for s in summaries])
        for k in host_keys
    }
    return {"metrics": avg_metrics, "host_features": avg_host}


def _get_val(summary: dict[str, Any], key: str) -> float:
    """Look up a metric from either sub-dict of a daemon summary."""
    v = summary.get("metrics", {}).get(key)
    if v is None:
        v = summary.get("host_features", {}).get(key, 0.0)
    return float(v) if v is not None else 0.0


def metrics_to_feature_vec(summary: dict[str, Any]) -> list[float]:
    return [min(_get_val(summary, k) / norm, 1.0) for k, norm in _FEATURE_MAP]


def compute_absolute_reward(summary: dict[str, Any]) -> float:
    """Scalar reward from an averaged daemon summary. Higher is better."""
    rq_lat  = min(_get_val(summary, "rq_latency_p95_us")           / 10_000.0, 1.0)
    dr_rate = min(_get_val(summary, "direct_reclaim_rate_per_sec") / 100.0,    1.0)
    cpu     = min(_get_val(summary, "host_cpu_busy_ratio"),                     1.0)
    dirty   = min(_get_val(summary, "host_dirty_kb")               / 200_000.0, 1.0)
    mem_avail = _get_val(summary, "host_mem_available_ratio")

    return round(
        0.35 * mem_avail
        - 0.30 * rq_lat
        - 0.20 * dr_rate
        - 0.10 * cpu
        - 0.05 * dirty,
        6,
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

    If the cluster has fewer points than MIN_FIT we fall back to argmax
    over the raw observations (GP would overfit anyway).
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

    # Random search over 2000 candidates from the full space (grid search is
    # intractable for 8+ dimensions), pick the config with the highest GP mean.
    gp = opt.models[-1]
    candidates = opt.space.rvs(n_samples=2000, random_state=42)
    X_t = opt.space.transform(candidates)
    mean_neg, _ = gp.predict(X_t, return_std=True)
    best_i = int(np.argmin(mean_neg))   # minimising negated reward
    return list(candidates[best_i])


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
            loaded = joblib.load(self.model_path)
            if len(loaded.space.dimensions) == len(space):
                self.opt: Optimizer = loaded
                print(f"Resumed GP model: {self.model_path} "
                      f"({len(self.opt.Xi)} prior observations)")
            else:
                print(f"Discarding stale GP model (was {len(loaded.space.dimensions)}-dim, "
                      f"now {len(space)}-dim) — starting fresh.")
                self.opt = Optimizer(
                    dimensions=space,
                    base_estimator="GP",
                    acq_func="EI",
                    n_initial_points=6,
                    random_state=42,
                )
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

        # Create a cgroup for the stressor so eBPF only tracks its events,
        # not everything else running on the machine.
        cgroup_dir, cgid = (None, None) if self.dry_run else _create_stressor_cgroup()

        summary_file = Path(tempfile.mktemp(suffix=".jsonl", prefix="reflex_tune_"))
        daemon = DaemonContext(summary_file, dry_run=self.dry_run, cgroup_id=cgid)
        daemon.start()

        proc = self._start_stressor()
        if proc is not None and cgroup_dir is not None:
            _move_to_cgroup(proc.pid, cgroup_dir)

        if self.warmup > 0:
            time.sleep(self.warmup)

        measure_start = time.time()
        time.sleep(self.duration)

        self._stop_stressor(proc)
        daemon.stop()
        _cleanup_cgroup(cgroup_dir)

        summary = _read_avg_summary(summary_file, after_ts=measure_start)
        summary_file.unlink(missing_ok=True)

        self.restore_baseline()

        reward = compute_absolute_reward(summary)
        self.opt.tell(config, -reward)

        if reward > self.best_reward:
            self.best_reward = reward
            self.best_config = [int(v) for v in config]

        _print_result(reward, summary)
        return {
            "feature_vec": metrics_to_feature_vec(summary),
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

def _print_result(reward: float, summary: dict[str, Any]) -> None:
    parts = [f"reward={reward:+.4f}"]
    mem = _get_val(summary, "host_mem_available_ratio")
    if mem:
        parts.append(f"mem_avail={mem:.2%}")
    rq = _get_val(summary, "rq_latency_p95_us")
    if rq:
        parts.append(f"rq_p95={rq:.0f}us")
    dr = _get_val(summary, "direct_reclaim_rate_per_sec")
    if dr:
        parts.append(f"dr_rate={dr:.1f}/s")
    blk = _get_val(summary, "blk_latency_p95_us")
    if blk:
        parts.append(f"blk_p95={blk:.0f}us")
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
        "--skip-cluster", action="store_true",
        help="Skip k-means clustering at the end (use when more workloads follow)",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Skip sysctl writes, stressor, and daemon; useful for testing the loop",
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

    if not args.skip_cluster:
        cluster_and_save_library(
            experiments_path, library_path,
            catalog_path=args.catalog,
            max_k=args.max_clusters,
        )
    else:
        print("Skipping clustering (--skip-cluster set) — run cluster step after all workloads complete.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
