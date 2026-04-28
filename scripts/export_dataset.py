#!/usr/bin/env python3
"""
Export (state, action, reward) training rows from one or more Reflex run directories.

Usage:
    python3 scripts/export_dataset.py data/runs/run-*/  [options]

    --output PATH       CSV output path (default: data/dataset.csv)
    --outcome-lag N     Windows after action to measure outcome (default: 3)
    --reward-weights    Comma-separated w_rq95,w_err,w_cpu (default: 0.5,0.3,0.2)

Output schema (one row per applied action):
    run_id, window_id, timestamp
    -- state at action window --
    s_rq_latency_p50_us, s_rq_latency_p95_us, s_rq_latency_p99_us,
    s_context_switch_rate, s_syscall_error_rate,
    s_cpu_busy, s_mem_available, s_dirty_kb, s_loadavg,
    -- action --
    tuner_id, action_id, target, value_before, value_after,
    -- outcome (state N windows later) --
    o_rq_latency_p50_us, o_rq_latency_p95_us, o_rq_latency_p99_us,
    o_context_switch_rate, o_syscall_error_rate,
    o_cpu_busy, o_mem_available, o_dirty_kb, o_loadavg,
    -- deltas (outcome - state, normalized by state where sensible) --
    d_rq_latency_p95_us, d_rq_latency_p99_us,
    d_syscall_error_rate, d_cpu_busy, d_mem_available,
    -- reward --
    reward,
    -- metadata --
    was_rolled_back, rollback_reason, outcome_available
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path
from typing import Any


STATE_METRIC_KEYS = [
    "rq_latency_p50_us",
    "rq_latency_p95_us",
    "rq_latency_p99_us",
    "context_switch_rate_per_sec",
    "syscall_error_rate",
]

STATE_HOST_KEYS = [
    "host_cpu_busy_ratio",
    "host_mem_available_ratio",
    "host_dirty_kb",
    "host_loadavg_1m",
]

STATE_KEYS = STATE_METRIC_KEYS + STATE_HOST_KEYS

DELTA_KEYS = [
    "rq_latency_p95_us",
    "rq_latency_p99_us",
    "syscall_error_rate",
    "host_cpu_busy_ratio",
    "host_mem_available_ratio",
]


def compute_reward(
    state: dict[str, float],
    outcome: dict[str, float],
    weights: tuple[float, float, float],
) -> float:
    """
    Reward = improvement in weighted composite metric.
    Positive = better, negative = worse.

    Components (all normalized to [0,1] range via expected maxima):
      w_rq95  * delta(rq_latency_p95_us)   / 10000
      w_err   * delta(syscall_error_rate)
      w_cpu   * delta(host_cpu_busy_ratio)
    """
    w_rq95, w_err, w_cpu = weights
    d_rq95 = (state.get("rq_latency_p95_us", 0) - outcome.get("rq_latency_p95_us", 0)) / 10000.0
    d_err  = (state.get("syscall_error_rate", 0)  - outcome.get("syscall_error_rate", 0))
    d_cpu  = (state.get("host_cpu_busy_ratio", 0) - outcome.get("host_cpu_busy_ratio", 0))
    return round(w_rq95 * d_rq95 + w_err * d_err + w_cpu * d_cpu, 6)


def extract_state(summary: dict[str, Any]) -> dict[str, float]:
    metrics = summary.get("metrics", {})
    host    = summary.get("host_features", {})
    state: dict[str, float] = {}
    for k in STATE_METRIC_KEYS:
        state[k] = float(metrics.get(k, 0))
    for k in STATE_HOST_KEYS:
        state[k] = float(host.get(k, 0))
    return state


def load_summaries(run_dir: Path) -> list[dict[str, Any]]:
    path = run_dir / "summary.jsonl"
    summaries = []
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                summaries.append(json.loads(line))
    return summaries


def load_decisions(run_dir: Path) -> dict[int, dict[str, Any]]:
    """Returns dict of window_id -> {decision?, action_apply?, rollback?}."""
    path = run_dir / "decisions.jsonl"
    windows: dict[int, dict[str, Any]] = {}
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            wid = rec.get("window_id")
            if wid is None:
                continue
            if wid not in windows:
                windows[wid] = {}
            rt = rec.get("record_type")
            if rt in ("decision", "action_apply", "rollback"):
                windows[wid][rt] = rec
    return windows


def process_run(
    run_dir: Path,
    outcome_lag: int,
    weights: tuple[float, float, float],
) -> list[dict[str, Any]]:
    summaries = load_summaries(run_dir)
    windows   = load_decisions(run_dir)
    run_id    = run_dir.name
    rows = []

    for wid, wdata in sorted(windows.items()):
        apply_rec = wdata.get("action_apply")
        if apply_rec is None:
            continue

        # summaries are 0-indexed; window_id is 1-indexed
        summary_idx = wid - 1
        outcome_idx = summary_idx + outcome_lag

        if summary_idx < 0 or summary_idx >= len(summaries):
            continue

        state   = extract_state(summaries[summary_idx])
        outcome_available = outcome_idx < len(summaries)
        outcome = extract_state(summaries[outcome_idx]) if outcome_available else {}

        rollback_rec  = wdata.get("rollback")
        was_rolled_back  = rollback_rec is not None and rollback_rec.get("rollback_ok", False)
        rollback_reason  = rollback_rec.get("reason", "") if rollback_rec else ""

        deltas: dict[str, float] = {}
        if outcome_available:
            for k in DELTA_KEYS:
                deltas[k] = round(outcome.get(k, 0) - state.get(k, 0), 6)

        reward = compute_reward(state, outcome, weights) if outcome_available else None

        row: dict[str, Any] = {
            "run_id":    run_id,
            "window_id": wid,
            "timestamp": apply_rec.get("timestamp"),
        }
        for k in STATE_KEYS:
            row[f"s_{k}"] = state.get(k, "")
        row["tuner_id"]     = apply_rec.get("tuner_id")
        row["action_id"]    = apply_rec.get("action_id")
        row["target"]       = apply_rec.get("target")
        row["value_before"] = apply_rec.get("previous_value")
        row["value_after"]  = apply_rec.get("value")
        for k in STATE_KEYS:
            row[f"o_{k}"] = outcome.get(k, "") if outcome_available else ""
        for k in DELTA_KEYS:
            row[f"d_{k}"] = deltas.get(k, "")
        row["reward"]           = reward if reward is not None else ""
        row["was_rolled_back"]  = was_rolled_back
        row["rollback_reason"]  = rollback_reason
        row["outcome_available"] = outcome_available

        rows.append(row)

    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description="Export Reflex run data as training dataset.")
    parser.add_argument("run_dirs", nargs="+", type=Path, help="Run directories to process")
    parser.add_argument("--output", type=Path,
                        default=Path(__file__).resolve().parent.parent / "data" / "dataset.csv")
    parser.add_argument("--outcome-lag", type=int, default=3,
                        help="Windows after action to measure outcome (default: 3)")
    parser.add_argument("--reward-weights", default="0.5,0.3,0.2",
                        help="w_rq95,w_err,w_cpu (default: 0.5,0.3,0.2)")
    args = parser.parse_args()

    try:
        weights = tuple(float(x) for x in args.reward_weights.split(","))
        assert len(weights) == 3
    except Exception:
        print("--reward-weights must be three comma-separated floats", file=sys.stderr)
        return 1

    all_rows: list[dict[str, Any]] = []
    for run_dir in args.run_dirs:
        run_dir = run_dir.resolve()
        if not (run_dir / "decisions.jsonl").exists():
            print(f"Skipping {run_dir}: no decisions.jsonl", file=sys.stderr)
            continue
        rows = process_run(run_dir, args.outcome_lag, weights)
        print(f"{run_dir.name}: {len(rows)} action rows")
        all_rows.extend(rows)

    if not all_rows:
        print("No action rows found.", file=sys.stderr)
        return 1

    args.output.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = list(all_rows[0].keys())
    with args.output.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(all_rows)

    print(f"\nWrote {len(all_rows)} rows to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
