#!/usr/bin/env python3
"""Human-readable compact tables for scorecard_three_way JSON (single run or trial median aggregate)."""
from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path
from typing import Any


def _fmt_num(x: Any, *, width: int = 10) -> str:
    if x is None:
        return "-".rjust(width)
    if isinstance(x, (int, float)) and (math.isnan(x) or math.isinf(x)):
        return "nan".rjust(width)
    if isinstance(x, float):
        ax = abs(x)
        if ax >= 1000 or (ax > 0 and ax < 1e-3):
            s = f"{x:.3e}"
        elif ax >= 100:
            s = f"{x:.1f}"
        elif ax >= 10:
            s = f"{x:.2f}"
        else:
            s = f"{x:.4f}".rstrip("0").rstrip(".")
        return s[:width].rjust(width)
    return str(x)[:width].rjust(width)


def _abbr(mode: str) -> str:
    if mode == "heuristic":
        return "heur"
    if mode == "workload_only":
        return "work"
    return mode[:4]


def format_plain_scorecard(doc: dict[str, Any]) -> str:
    """Single-run scorecard (per-trial JSON from scorecard_three_way.py)."""
    lines: list[str] = []
    opts = doc.get("scorecard_options") or {}
    lines.append("Reflex scorecard (single run)")
    if opts:
        lines.append(f"options: {opts}")
    lines.append("")
    for comp in doc.get("comparisons", []):
        base = str(comp.get("base", ""))
        other = str(comp.get("other", ""))
        bk = f"{base}_mean"
        ok = f"{other}_mean"
        lines.append(f"=== {base} vs {other} ===")
        header = f"{'metric':<42} {_abbr(base):>6}_mean {_abbr(other):>6}_mean {'d%':>8}"
        lines.append(header)
        lines.append("-" * len(header))
        for m in comp.get("metrics", []):
            name = str(m.get("name", ""))
            if len(name) > 42:
                name = name[:39] + "..."
            lines.append(
                f"{name:<42} {_fmt_num(m.get(bk), width=8)} {_fmt_num(m.get(ok), width=8)} {_fmt_num(m.get('delta_percent'), width=8)}"
            )
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def format_aggregate_scorecard(doc: dict[str, Any]) -> str:
    """Trial median aggregate from scorecard_trials_aggregate.aggregate_median."""
    lines: list[str] = []
    trials = doc.get("trials", "?")
    lines.append(f"Reflex scorecard — medians over {trials} trials (min/max in JSON)")
    opts = doc.get("scorecard_options") or {}
    if opts:
        lines.append(f"options: {opts}")
    lines.append("")
    for comp in doc.get("comparisons", []):
        base = str(comp.get("base", ""))
        other = str(comp.get("other", ""))
        bm = f"{base}_mean_median"
        om = f"{other}_mean_median"
        dm = "delta_percent_median"
        lines.append(f"=== {base} vs {other} ===")
        hdr = f"{'metric':<40} {_abbr(base)}_μ_med {_abbr(other)}_μ_med {'d%_med':>10}"
        lines.append(hdr)
        lines.append("-" * len(hdr))
        for m in comp.get("metrics", []):
            name = str(m.get("name", ""))
            if len(name) > 40:
                name = name[:37] + "..."
            lines.append(
                f"{name:<40} {_fmt_num(m.get(bm), width=10)} {_fmt_num(m.get(om), width=10)} {_fmt_num(m.get(dm), width=10)}"
            )
        lines.append("")
    lines.append(
        "Legend: *_μ_med = median of per-trial mean(metric). Per-trial min/max for means and deltas stay in the .json file."
    )
    return "\n".join(lines)


def format_scorecard_table(doc: dict[str, Any]) -> str:
    if doc.get("aggregate") == "median_min_max":
        return format_aggregate_scorecard(doc)
    return format_plain_scorecard(doc)


def write_compact_summary_sidecar(json_path: Path, doc: dict[str, Any] | None = None) -> Path:
    """Write `<stem>.summary.txt` next to the JSON; return path to summary."""
    path = Path(json_path)
    payload = doc if doc is not None else json.loads(path.read_text(encoding="utf-8"))
    out = path.with_suffix(".summary.txt")
    out.write_text(format_scorecard_table(payload), encoding="utf-8")
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="Render scorecard JSON as compact .summary.txt")
    parser.add_argument("json_path", type=Path, help="Path to scorecard_three_way*.json")
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        help="Write summary here (default: alongside JSON with .summary.txt)",
    )
    args = parser.parse_args()
    doc = json.loads(args.json_path.read_text(encoding="utf-8"))
    text = format_scorecard_table(doc)
    out = args.output or args.json_path.with_suffix(".summary.txt")
    out.write_text(text, encoding="utf-8")
    print(text, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
