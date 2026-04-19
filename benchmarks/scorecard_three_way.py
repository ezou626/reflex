#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import statistics
import sys
from pathlib import Path
from typing import Any


def _numeric_floats(d: Any) -> dict[str, float]:
    if not isinstance(d, dict):
        return {}
    out: dict[str, float] = {}
    for key, val in d.items():
        if isinstance(val, (int, float)):
            out[str(key)] = float(val)
    return out


def _load_run_metadata(summary_path: Path) -> dict[str, Any] | None:
    meta_path = summary_path.parent / "run_metadata.json"
    if not meta_path.is_file():
        return None
    try:
        raw = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return raw if isinstance(raw, dict) else None


def _window_overlaps_workload(rec: dict[str, Any], w_start: float, w_end: float) -> bool:
    try:
        rs = float(rec.get("window_start_unix_s", 0.0))
        re_ = float(rec.get("window_end_unix_s", 0.0))
    except (TypeError, ValueError):
        return False
    return re_ >= w_start and rs <= w_end


def _iter_window_records(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            if rec.get("record_type") not in (None, "window_summary"):
                continue
            rows.append(rec)
    return rows


def load_means(
    path: Path,
    *,
    drop_first: int = 0,
    drop_last: int = 0,
    workload_start: float | None = None,
    workload_end: float | None = None,
) -> dict[str, float]:
    """Mean per numeric key across selected window_summary lines.

    Merges ``metrics`` with scalar numeric ``host_features``. Optional workload
    interval uses overlap with ``[workload_start, workload_end]``.
    """
    records = _iter_window_records(path)
    if workload_start is not None and workload_end is not None:
        records = [r for r in records if _window_overlaps_workload(r, workload_start, workload_end)]
    if drop_first > 0:
        records = records[drop_first:]
    if drop_last > 0:
        records = records[:-drop_last] if drop_last < len(records) else []
    values: dict[str, list[float]] = {}
    for rec in records:
        merged: dict[str, float] = {}
        merged.update(_numeric_floats(rec.get("metrics")))
        for k, v in _numeric_floats(rec.get("host_features")).items():
            if k not in merged:
                merged[k] = v
        for key, val in merged.items():
            values.setdefault(key, []).append(val)
    return {k: statistics.fmean(v) for k, v in values.items() if v}


def delta_percent(base: float, other: float) -> float:
    denom = abs(base) if abs(base) > 1e-9 else 1.0
    return ((other - base) / denom) * 100.0


def _include_metric_key(name: str, *, include_psi_totals: bool) -> bool:
    if include_psi_totals:
        return True
    return not name.endswith("_total")


def pairwise(
    name_a: str,
    a: dict[str, float],
    name_b: str,
    b: dict[str, float],
    *,
    include_psi_totals: bool,
) -> dict[str, object]:
    keys = sorted(k for k in (set(a) & set(b)) if _include_metric_key(k, include_psi_totals=include_psi_totals))
    metrics = []
    for k in keys:
        metrics.append(
            {
                "name": k,
                f"{name_a}_mean": round(a[k], 6),
                f"{name_b}_mean": round(b[k], 6),
                "delta_percent": round(delta_percent(a[k], b[k]), 4),
            }
        )
    return {
        "base": name_a,
        "other": name_b,
        "metrics": metrics,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Pairwise mean deltas across three summary.jsonl runs (heuristic, noop, workload_only)."
    )
    parser.add_argument(
        "summaries",
        nargs=3,
        metavar=("HEURISTIC", "NOOP", "WORKLOAD_ONLY"),
        help="Paths to summary.jsonl for each mode.",
    )
    parser.add_argument(
        "--drop-first",
        type=int,
        default=0,
        help="After filtering, drop the first N window lines per file.",
    )
    parser.add_argument(
        "--drop-last",
        type=int,
        default=0,
        help="After filtering, drop the last M window lines per file.",
    )
    parser.add_argument(
        "--filter-workload-window",
        action="store_true",
        help="Keep only windows overlapping workload_started_unix_s..workload_ended_unix_s from run_metadata.json next to each summary.",
    )
    parser.add_argument(
        "--include-psi-totals",
        action="store_true",
        help="Include keys ending in _total (cumulative PSI counters); excluded by default.",
    )
    args = parser.parse_args()

    heur_path = Path(args.summaries[0])
    noop_path = Path(args.summaries[1])
    wo_path = Path(args.summaries[2])

    def means_for(path: Path) -> dict[str, float]:
        ws: float | None = None
        we: float | None = None
        if args.filter_workload_window:
            meta = _load_run_metadata(path)
            if meta:
                try:
                    ws = float(meta["workload_started_unix_s"])
                    we = float(meta["workload_ended_unix_s"])
                except (KeyError, TypeError, ValueError):
                    ws, we = None, None
            if ws is None or we is None:
                print(
                    f"[scorecard] warning: --filter-workload-window but missing timestamps in "
                    f"{path.parent / 'run_metadata.json'}; using all windows for {path.name}",
                    file=sys.stderr,
                )
        return load_means(
            path,
            drop_first=args.drop_first,
            drop_last=args.drop_last,
            workload_start=ws,
            workload_end=we,
        )

    heur = means_for(heur_path)
    noop = means_for(noop_path)
    wo = means_for(wo_path)

    out: dict[str, Any] = {
        "heuristic": str(heur_path),
        "noop": str(noop_path),
        "workload_only": str(wo_path),
        "scorecard_options": {
            "drop_first": args.drop_first,
            "drop_last": args.drop_last,
            "filter_workload_window": bool(args.filter_workload_window),
            "include_psi_totals": bool(args.include_psi_totals),
        },
        "comparisons": [
            pairwise("heuristic", heur, "noop", noop, include_psi_totals=args.include_psi_totals),
            pairwise("heuristic", heur, "workload_only", wo, include_psi_totals=args.include_psi_totals),
            pairwise("noop", noop, "workload_only", wo, include_psi_totals=args.include_psi_totals),
        ],
    }
    print(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
