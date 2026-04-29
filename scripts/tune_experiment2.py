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
import subprocess
import time
from pathlib import Path

import numpy as np
from skopt import Optimizer
from skopt.space import Integer

from daemon_core.tuners.loaders import load_tuner_catalog
from daemon_core.tuners.schema import TunerCatalogEntry
from daemon_core.tuners.sysctl_util import read_sysctl, sysctl_name_to_path, write_sysctl
from implementations.aggregators.current_payload import _PAYLOAD_SIZE, decode_payload

REPO = Path(__file__).resolve().parent.parent
LOADER = REPO / "implementations" / "ebpf" / "build" / "reflex"
CATALOG = REPO / "configs" / "tuner_catalog.yaml"
EXPERIMENTS = (
    REPO
    / "implementations"
    / "controllers"
    / "workload_classifier"
    / "models"
    / "experiments.jsonl"
)


def load_tuners(catalog_path: Path) -> list[TunerCatalogEntry]:
    """Enabled runtime_sysctl int knobs with bounds — these are our search dims."""
    cat = load_tuner_catalog(catalog_path)
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


def measure(loader: subprocess.Popen[bytes], seconds: float) -> dict[str, float]:
    """Drain loader payloads for `seconds`; return metrics for the reward function."""
    deadline = time.time() + seconds
    rq_latency_us: list[int] = []
    blk_latency_us: list[int] = []
    fails = 0
    syscalls = 0
    psi_mem = _psi_some_avg10("memory")
    psi_io  = _psi_some_avg10("io")
    psi_cpu = _psi_some_avg10("cpu")
    t0 = time.time()
    while time.time() < deadline:
        if loader.stdout is None:
            break
        buf = loader.stdout.read(_PAYLOAD_SIZE)
        if not buf or len(buf) < _PAYLOAD_SIZE:
            break
        event = decode_payload(buf)
        if event["event_name"] == "rq_latency":
            rq_latency_us.append(int(event["rq_latency_us"]))
        elif event["event_name"] == "blk_latency":
            blk_latency_us.append(int(event["blk_lat_us"]))
        elif event["event_name"] == "sys_exit":
            syscalls += 1
        if event.get("is_error", False):
            fails += 1
    elapsed = max(time.time() - t0, 1e-6)
    # Average PSI across the window: pre + post / 2 is good enough for a noisy signal.
    psi_mem = (psi_mem + _psi_some_avg10("memory")) / 2.0
    psi_io  = (psi_io  + _psi_some_avg10("io"))     / 2.0
    psi_cpu = (psi_cpu + _psi_some_avg10("cpu"))    / 2.0
    return {
        "p95_latency": float(np.percentile(rq_latency_us, 95)) if rq_latency_us else 0.0,
        "blk_p95_latency": (
            float(np.percentile(blk_latency_us, 95)) if blk_latency_us else 0.0
        ),
        "throughput":  syscalls / elapsed,
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
    ap.add_argument("--loader", type=Path, default=LOADER)
    ap.add_argument("--catalog", type=Path, default=CATALOG)
    ap.add_argument("--experiments-path", type=Path, default=EXPERIMENTS)
    args = ap.parse_args()

    tuners   = load_tuners(args.catalog)
    space    = [Integer(int(t.min_value), int(t.max_value), name=t.id) for t in tuners]
    baseline = {t.id: int(read_sysctl(sysctl_name_to_path(t.sysctl), t.kind)) for t in tuners}

    opt = Optimizer(space, base_estimator="GP", acq_func="EI",
                    n_initial_points=5, random_state=42)

    # Loader is the eBPF metric source — spawn once and keep it running.
    loader = subprocess.Popen([str(args.loader), str(0)], stdout=subprocess.PIPE)

    # Append-mode JSONL: every experiment becomes one line for cross-session analysis.
    args.experiments_path.parent.mkdir(parents=True, exist_ok=True)
    series = args.experiments_path.open("a", encoding="utf-8", buffering=1)

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
