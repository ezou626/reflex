from __future__ import annotations

from typing import Any

import numpy as np

# (metric_key, normalization_denominator)
# Keys must exist in summary["metrics"] or summary["host_features"]
FEATURE_SPEC: list[tuple[str, float]] = [
    ("rq_latency_p95_us",            10_000.0),
    ("rq_latency_p99_us",            10_000.0),
    ("context_switch_rate_per_sec",  100_000.0),
    ("syscall_error_rate",           1.0),
    ("host_cpu_busy_ratio",          1.0),
    ("host_mem_available_ratio",     1.0),
    ("host_dirty_kb",                200_000.0),
    ("host_loadavg_1m",              10.0),
    ("direct_reclaim_rate_per_sec",  100.0),    # >10/s = severe memory pressure
    ("direct_reclaim_lat_p95_us",    500_000.0),
    ("blk_latency_p95_us",           50_000.0),
]

N_FEATURES = len(FEATURE_SPEC)

# Default reward weights: (w_rq95, w_syscall_err, w_cpu_busy)
# Positive reward = improvement; negative = regression.
DEFAULT_WEIGHTS: tuple[float, float, float] = (0.5, 0.3, 0.2)


def _get_val(summary: dict[str, Any], key: str) -> float:
    v = summary.get("metrics", {}).get(key)
    if v is not None:
        return float(v)
    v = summary.get("host_features", {}).get(key)
    return float(v) if v is not None else 0.0


def extract_state_vec(summary: dict[str, Any]) -> np.ndarray:
    return np.array(
        [_get_val(summary, k) / denom for k, denom in FEATURE_SPEC],
        dtype=np.float64,
    )


def compute_reward(
    before: dict[str, Any],
    after: dict[str, Any],
    weights: tuple[float, float, float] = DEFAULT_WEIGHTS,
) -> float:
    w_rq95, w_err, w_cpu = weights
    d_rq95 = (_get_val(before, "rq_latency_p95_us") - _get_val(after, "rq_latency_p95_us")) / 10_000.0
    d_err  = _get_val(before, "syscall_error_rate")  - _get_val(after, "syscall_error_rate")
    d_cpu  = _get_val(before, "host_cpu_busy_ratio") - _get_val(after, "host_cpu_busy_ratio")
    return round(w_rq95 * d_rq95 + w_err * d_err + w_cpu * d_cpu, 6)
