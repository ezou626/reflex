#!/usr/bin/env python3
"""
Minimal GP tuner: load daemon_core tuners, spawn the implementation-local
loader, and run a controlled experiment loop.

Loop per iteration:
    ask GP → write sysctls → measure N seconds of loader payloads → tell GP
"""
from __future__ import annotations

import argparse
import json
import os
import queue
import shlex
import struct
import subprocess
import sys
import threading
import time
from pathlib import Path

import numpy as np
from skopt import Optimizer
from skopt.space import Integer

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from daemon_core.tuners.loaders import load_tuner_catalog  # noqa: E402
from daemon_core.tuners.sysctl_util import (  # noqa: E402
    read_sysctl,
    sysctl_name_to_path,
    write_sysctl,
)

LOADER       = REPO / "build" / "loader2"
CATALOG      = REPO / "configs" / "tuner_catalog.yaml"
MODELS_DIR   = REPO / "implementations" / "controllers" / "workload_classifier" / "models"
EXPERIMENTS  = MODELS_DIR / "experiments.jsonl"

# struct summary from src/loader2.c (1 record/sec):
#   window_end_ns u64, rq_p95_us u32, syscall_count u32, failure_count u32,
#   blk_p95_us u32, ctx_switch_count u32, direct_reclaim_count u32, fork_count u32
SUMMARY_FMT  = "=QIIIIIII"
SUMMARY_SIZE = struct.calcsize(SUMMARY_FMT)


def make_cgroup() -> tuple[Path, int]:
    """Create a fresh cgroup for the stressor; return (dir, cgroup_id)."""
    d = Path(f"/sys/fs/cgroup/reflex_tune_{os.getpid()}")
    d.mkdir(exist_ok=True)
    return d, d.stat().st_ino


def load_tuners():
    """Enabled runtime_sysctl int knobs with bounds — these are our search dims."""
    cat = load_tuner_catalog(CATALOG)
    return [
        e for e in cat.tuners
        if e.enabled and e.scope == "runtime_sysctl" and e.kind == "int"
        and e.min_value is not None and e.max_value is not None
    ]


def _psi_some_avg10(resource: str) -> float:
    """Parse /proc/pressure/<resource> → 'some avg10' value."""
    try:
        for line in Path(f"/proc/pressure/{resource}").read_text().splitlines():
            if line.startswith("some "):
                for tok in line.split():
                    if tok.startswith("avg10="):
                        return float(tok.split("=", 1)[1])
    except OSError:
        pass
    return 0.0


def start_reader(loader: subprocess.Popen) -> queue.Queue:
    """Background thread drains loader2 summary records into a queue."""
    q: queue.Queue = queue.Queue()
    def _reader() -> None:
        assert loader.stdout is not None
        while True:
            buf = loader.stdout.read(SUMMARY_SIZE)
            if not buf or len(buf) < SUMMARY_SIZE:
                break
            q.put(struct.unpack(SUMMARY_FMT, buf))
    threading.Thread(target=_reader, daemon=True).start()
    return q


def measure(ev_q: queue.Queue, seconds: float) -> dict[str, float]:
    """Pull 1Hz summary records for `seconds`; return aggregated metrics for the reward function."""
    deadline = time.time() + seconds
    rq_p95s: list[int]  = []
    blk_p95s: list[int] = []
    syscalls            = 0
    fails               = 0
    ctx_switches        = 0
    direct_reclaims     = 0
    forks               = 0
    psi_mem = _psi_some_avg10("memory")
    psi_io  = _psi_some_avg10("io")
    psi_cpu = _psi_some_avg10("cpu")
    t0 = time.time()
    while True:
        remaining = deadline - time.time()
        if remaining <= 0:
            break
        try:
            (_ts, rq_p95_us, sys_n, fail_n,
             blk_p95_us, ctx_n, reclaim_n, fork_n) = ev_q.get(timeout=remaining)
        except queue.Empty:
            break
        if rq_p95_us:
            rq_p95s.append(rq_p95_us)
        if blk_p95_us:
            blk_p95s.append(blk_p95_us)
        syscalls        += sys_n
        fails           += fail_n
        ctx_switches    += ctx_n
        direct_reclaims += reclaim_n
        forks           += fork_n
    elapsed = max(time.time() - t0, 1e-6)
    # Average PSI across the window: pre + post / 2 is good enough for a noisy signal.
    psi_mem = (psi_mem + _psi_some_avg10("memory")) / 2.0
    psi_io  = (psi_io  + _psi_some_avg10("io"))     / 2.0
    psi_cpu = (psi_cpu + _psi_some_avg10("cpu"))    / 2.0
    return {
        # Reward-function features (don't change names — reward_fn reads these).
        "p95_latency": float(np.mean(rq_p95s)) if rq_p95s else 0.0,
        "throughput":  syscalls / elapsed,
        "mem":         psi_mem,
        "io":          psi_io,
        "cpu":         psi_cpu,
        "failures":    fails / elapsed,
        # Extra clustering-only features. Reward function ignores these.
        "blk_p95_latency":    float(np.mean(blk_p95s)) if blk_p95s else 0.0,
        "ctx_switch_rate":    ctx_switches / elapsed,
        "direct_reclaim_rate": direct_reclaims / elapsed,
        "fork_rate":          forks / elapsed,
    }


def reward_fn(base: dict[str, float], tuned: dict[str, float]) -> tuple[float, dict[str, float]]:
    """Ratio-based reward vs. baseline. Returns (total, per-term breakdown)."""
    eps = 1e-6
    parts = {
        "lat":  1.5 * (base["p95_latency"] / max(tuned["p95_latency"], eps)),
        "thru": 1.0 * (tuned["throughput"] / max(base["throughput"], eps)),
        "mem": -0.4 * (tuned["mem"] / max(base["mem"], eps)),
        "io":  -0.4 * (tuned["io"]  / max(base["io"],  eps)),
        "cpu": -0.2 * (tuned["cpu"] / max(base["cpu"], eps)),
        "fail":-2.0 * tuned["failures"],
    }
    return sum(parts.values()), parts


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--stressor", type=str, required=True,
                    help='Workload command, e.g. --stressor "stress-ng --vm 2 --vm-bytes 75%"')
    ap.add_argument("--experiments", type=int, default=20)
    ap.add_argument("--duration", type=float, default=10.0)
    ap.add_argument("--loader", type=Path, default=LOADER)
    ap.add_argument("--catalog", type=Path, default=CATALOG)
    ap.add_argument("--experiments-path", type=Path, default=EXPERIMENTS)
    args = ap.parse_args()

    stressor_cmd = shlex.split(args.stressor)

    tuners   = load_tuners()
    space    = [Integer(int(t.min_value), int(t.max_value), name=t.id) for t in tuners] # to feed into the scikit learn optimizer
    baseline = {t.id: int(read_sysctl(sysctl_name_to_path(t.sysctl), t.kind)) for t in tuners}

    opt = Optimizer(space, base_estimator="GP", acq_func="EI",
                    n_initial_points=5, random_state=42)

    # Stressor lives in its own cgroup; loader whitelists that cgid so eBPF
    # only collects events generated by the workload — not the whole host.
    cgroup_dir, cgid = make_cgroup()
    stressor = subprocess.Popen(stressor_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    (cgroup_dir / "cgroup.procs").write_text(str(stressor.pid))

    # Loader is the eBPF metric source — spawn once with our cgid filter.
    # stderr → log file so BPF attach failures are diagnosable instead of silent.
    MODELS_DIR.mkdir(parents=True, exist_ok=True)
    loader_log = (MODELS_DIR / "loader.log").open("a", buffering=1)
    loader = subprocess.Popen([str(LOADER), str(os.getpid()), str(cgid)],
                              stdout=subprocess.PIPE, stderr=loader_log)
    ev_q   = start_reader(loader)

    # Append-mode JSONL: every experiment becomes one line for cross-session analysis.
    args.experiments_path.parent.mkdir(parents=True, exist_ok=True)
    series = args.experiments_path.open("a", encoding="utf-8", buffering=1)

    try:
        # Baseline pass with default sysctls — reward is computed as a ratio against this.
        base = measure(ev_q, args.duration)
        print(f"[baseline] {base}")

        for i in range(args.experiments):
            cfg = opt.ask()
            for t, v in zip(tuners, cfg):
                try:
                    write_sysctl(sysctl_name_to_path(t.sysctl), int(v), t.kind)
                except OSError as exc:
                    print(f"  [skip] {t.sysctl}={int(v)} rejected ({exc})")

            tuned          = measure(ev_q, args.duration)
            reward, parts  = reward_fn(base, tuned)
            opt.tell(cfg, -reward)  # skopt minimises → negate

            series.write(json.dumps({
                "config":  {t.id: int(v) for t, v in zip(tuners, cfg)},
                "metrics": tuned,
                "parts":   parts,
                "reward":  reward,
            }) + "\n")

            parts_str = "  ".join(f"{k}={v:+.2f}" for k, v in parts.items())
            print(f"[{i+1:3d}/{args.experiments}] reward={reward:+.3f}  ({parts_str})  cfg={cfg}")
    finally:
        series.close()
        # Always restore sysctls and stop loader + stressor.
        for t in tuners:
            try:
                write_sysctl(sysctl_name_to_path(t.sysctl), baseline[t.id], t.kind)
            except OSError as exc:
                print(f"  [skip-restore] {t.sysctl}={baseline[t.id]} ({exc})")
        loader.terminate(); loader.wait(timeout=5)
        loader_log.close()
        stressor.terminate(); stressor.wait(timeout=5)
        try: cgroup_dir.rmdir()
        except OSError: pass

    best_i = int(np.argmin(opt.yi))
    print(f"\nBest: reward={-opt.yi[best_i]:+.2f}  cfg={opt.Xi[best_i]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
