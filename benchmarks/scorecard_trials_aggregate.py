#!/usr/bin/env python3
"""Median (and min/max) of pairwise metrics across multiple scorecard_three_way.json files."""
from __future__ import annotations

import argparse
import json
import statistics
import sys
from pathlib import Path
from typing import Any


def aggregate_median(scorecard_paths: list[Path]) -> dict[str, Any]:
    if not scorecard_paths:
        raise ValueError("need at least one scorecard path")
    loaded: list[dict[str, Any]] = []
    for p in scorecard_paths:
        loaded.append(json.loads(p.read_text(encoding="utf-8")))
    n = len(loaded)
    num_comp = len(loaded[0]["comparisons"])
    for t in loaded[1:]:
        if len(t["comparisons"]) != num_comp:
            raise ValueError("scorecards have different comparison counts")

    comparisons_out: list[dict[str, Any]] = []
    for ci in range(num_comp):
        comp0 = loaded[0]["comparisons"][ci]
        base = comp0["base"]
        other = comp0["other"]
        for t in loaded:
            c = t["comparisons"][ci]
            if c["base"] != base or c["other"] != other:
                raise ValueError(f"trial mismatch: expected {base} vs {other}")

        by_name: dict[str, list[dict[str, Any]]] = {}
        for trial in loaded:
            for m in trial["comparisons"][ci]["metrics"]:
                name = m["name"]
                by_name.setdefault(name, []).append(m)

        merged_metrics: list[dict[str, Any]] = []
        for name in sorted(by_name):
            rows = by_name[name]
            if len(rows) != n:
                continue
            numeric_keys = [k for k in rows[0] if k != "name" and isinstance(rows[0][k], (int, float))]
            row_out: dict[str, Any] = {"name": name, "trials": n}
            for key in numeric_keys:
                vals = [float(r[key]) for r in rows]
                row_out[f"{key}_median"] = round(statistics.median(vals), 6)
                row_out[f"{key}_min"] = round(min(vals), 6)
                row_out[f"{key}_max"] = round(max(vals), 6)
            merged_metrics.append(row_out)

        comparisons_out.append(
            {
                "base": base,
                "other": other,
                "metrics": merged_metrics,
            }
        )

    return {
        "aggregate": "median_min_max",
        "trials": n,
        "trial_scorecards": [str(p) for p in scorecard_paths],
        "scorecard_options": loaded[0].get("scorecard_options", {}),
        "comparisons": comparisons_out,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Aggregate scorecard_three_way.json trials (median/min/max).")
    parser.add_argument("-o", "--output", type=Path, help="Write aggregate JSON to this path.")
    parser.add_argument(
        "--json-stdout",
        action="store_true",
        help="Print full JSON to stdout instead of the compact table (default is compact).",
    )
    parser.add_argument(
        "scorecards",
        nargs="+",
        type=Path,
        metavar="SCORECARD_JSON",
        help="One or more scorecard_three_way.json files (same trial ordering).",
    )
    args = parser.parse_args()
    out = aggregate_median([Path(p) for p in args.scorecards])
    text = json.dumps(out, indent=2, ensure_ascii=False)
    bench = Path(__file__).resolve().parent
    if str(bench) not in sys.path:
        sys.path.insert(0, str(bench))
    import scorecard_compact as sc  # noqa: PLC0415

    if args.output:
        args.output.write_text(text + "\n", encoding="utf-8")
        sc.write_compact_summary_sidecar(args.output, out)
    if args.json_stdout or not args.output:
        print(text)
    else:
        print(sc.format_scorecard_table(out), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
