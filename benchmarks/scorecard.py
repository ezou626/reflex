#!/usr/bin/env python3
from __future__ import annotations

import json
import statistics
import sys
from pathlib import Path


def load_means(path: Path) -> dict[str, float]:
    values: dict[str, list[float]] = {}
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            metrics = rec.get("metrics", {})
            for key, val in metrics.items():
                if isinstance(val, (int, float)):
                    values.setdefault(key, []).append(float(val))
    return {k: statistics.fmean(v) for k, v in values.items() if v}


def delta_percent(base: float, tuned: float) -> float:
    denom = abs(base) if abs(base) > 1e-9 else 1.0
    return ((tuned - base) / denom) * 100.0


def main() -> int:
    if len(sys.argv) != 3:
        print("usage: benchmarks/scorecard.py <baseline_summary.jsonl> <tuned_summary.jsonl>")
        return 1
    base_path = Path(sys.argv[1])
    tuned_path = Path(sys.argv[2])
    baseline = load_means(base_path)
    tuned = load_means(tuned_path)
    keys = sorted(set(baseline) & set(tuned))
    scorecard = {
        "baseline": str(base_path),
        "tuned": str(tuned_path),
        "metrics": [],
    }
    for key in keys:
        scorecard["metrics"].append(
            {
                "name": key,
                "baseline_mean": round(baseline[key], 6),
                "tuned_mean": round(tuned[key], 6),
                "delta_percent": round(delta_percent(baseline[key], tuned[key]), 4),
            }
        )
    print(json.dumps(scorecard, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
