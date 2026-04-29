#!/usr/bin/env python3
"""
Minimal BO tuner: load tuners → spawn loader.c → run GP loop.

Loop per iteration:
    ask GP → write sysctls → measure N seconds of loader payloads → tell GP
"""
from __future__ import annotations

import argparse
import json
import struct
import subprocess
import sys
import time
from pathlib import Path

import numpy as np
from skopt import Optimizer
from skopt.space import Integer

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "daemon"))

from config.loaders import load_tuner_catalog  # noqa: E402
from tuners.sysctl_util import read_sysctl, write_sysctl, sysctl_name_to_path  # noqa: E402

LOADER       = REPO / "build" / "loader"
CATALOG      = REPO / "configs" / "tuner_catalog.yaml"
EXPERIMENTS  = REPO / "models" / "experiments.jsonl"

# struct payload from src/loader.c: tid u32, pid u32, syscall_id u64,
# cgroup_id u64, ret_val i64, dur_ns u64  — packed
PAYLOAD_FMT  = "=IIQQqQ"
PAYLOAD_SIZE = struct.calcsize(PAYLOAD_FMT)


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


def measure(loader: subprocess.Popen, seconds: float) -> dict[str, float]:
    """Drain loader payloads for `seconds`; return metrics for the reward function."""
    deadline = time.time() + seconds
    durs: list[int] = []
    fails = 0
    psi_mem = _psi_some_avg10("memory")
    psi_io  = _psi_some_avg10("io")
    psi_cpu = _psi_some_avg10("cpu")
    t0 = time.time()
    while time.time() < deadline:
        buf = loader.stdout.read(PAYLOAD_SIZE)
        if not buf or len(buf) < PAYLOAD_SIZE:
            break
        _, _, _, _, ret_val, dur_ns = struct.unpack(PAYLOAD_FMT, buf)
        durs.append(dur_ns)
        if ret_val < 0:
            fails += 1
    elapsed = max(time.time() - t0, 1e-6)
    # Average PSI across the window: pre + post / 2 is good enough for a noisy signal.
    psi_mem = (psi_mem + _psi_some_avg10("memory")) / 2.0
    psi_io  = (psi_io  + _psi_some_avg10("io"))     / 2.0
    psi_cpu = (psi_cpu + _psi_some_avg10("cpu"))    / 2.0
    return {
        "p95_latency": float(np.percentile(durs, 95)) if durs else 0.0,
        "throughput":  len(durs) / elapsed,
        "mem":         psi_mem,
        "io":          psi_io,
        "cpu":         psi_cpu,
        "failures":    fails / elapsed,
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
    ap.add_argument("--experiments", type=int, default=20)
    ap.add_argument("--duration", type=float, default=10.0)
    args = ap.parse_args()

    tuners   = load_tuners()
    space    = [Integer(int(t.min_value), int(t.max_value), name=t.id) for t in tuners] # to feed into the scikit learn optimizer
    baseline = {t.id: int(read_sysctl(sysctl_name_to_path(t.sysctl), t.kind)) for t in tuners}

    opt = Optimizer(space, base_estimator="GP", acq_func="EI",
                    n_initial_points=5, random_state=42)

    # Loader is the eBPF metric source — spawn once and keep it running.
    loader = subprocess.Popen([str(LOADER), str(0)], stdout=subprocess.PIPE)

    # Append-mode JSONL: every experiment becomes one line for cross-session analysis.
    EXPERIMENTS.parent.mkdir(parents=True, exist_ok=True)
    series = EXPERIMENTS.open("a", encoding="utf-8", buffering=1)

    try:
        # Baseline pass with default sysctls — reward is computed as a ratio against this.
        base = measure(loader, args.duration)
        print(f"[baseline] {base}")

        for i in range(args.experiments):
            cfg = opt.ask()
            for t, v in zip(tuners, cfg):
                write_sysctl(sysctl_name_to_path(t.sysctl), int(v), t.kind)

            tuned          = measure(loader, args.duration)
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
        # Always restore sysctls and stop the loader.
        for t in tuners:
            write_sysctl(sysctl_name_to_path(t.sysctl), baseline[t.id], t.kind)
        loader.terminate()
        loader.wait(timeout=5)

    best_i = int(np.argmin(opt.yi))
    print(f"\nBest: reward={-opt.yi[best_i]:+.2f}  cfg={opt.Xi[best_i]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
