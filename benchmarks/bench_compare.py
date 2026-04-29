#!/usr/bin/env python3
"""
Compare UnixBench or LEBench scores across controller modes.

Usage:
    python3 benchmarks/bench_compare.py baseline:data/runs/RUN_A noop:data/runs/RUN_B classifier:data/runs/RUN_C

Each argument is LABEL:RUN_DIR. Reads workload.log from each run dir,
parses the benchmark output, and prints a comparison table.
Also writes bench_compare.json alongside the last scorecard in the batch.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _load_scores(run_dir: Path) -> tuple[str, dict[str, Any]]:
    sys.path.insert(0, str(_repo_root() / "benchmarks"))
    from parse_benchmark_scores import detect_and_parse  # noqa: PLC0415

    log = run_dir / "workload.log"
    if not log.is_file():
        return "missing", {}
    text = log.read_text(encoding="utf-8", errors="replace")
    return detect_and_parse(text)


def _fmt(v: Any, width: int = 12) -> str:
    if v is None:
        return "-".rjust(width)
    if isinstance(v, float):
        s = f"{v:.2f}"
    else:
        s = str(v)
    return s[:width].rjust(width)


def _delta_pct(base: float | None, other: float | None) -> str:
    if base is None or other is None or base == 0:
        return "-".rjust(8)
    d = (other - base) / abs(base) * 100
    sign = "+" if d >= 0 else ""
    return f"{sign}{d:.1f}%".rjust(8)


def _print_unixbench_table(
    labels: list[str], scores: list[dict[str, Any]], baseline_label: str
) -> None:
    composite = [s.get("composite_score") for s in scores]
    all_subtests: list[str] = []
    for s in scores:
        for k in s.get("subtests", {}):
            if k not in all_subtests:
                all_subtests.append(k)

    base_idx = labels.index(baseline_label) if baseline_label in labels else 0
    header = f"{'subtest':<42}" + "".join(f"{label[:10]:>12}" for label in labels) + "".join(
        f"  d%_vs_{labels[base_idx][:6]}" for label in labels if label != labels[base_idx]
    )
    print("UnixBench Index Scores")
    print("=" * len(header))
    print(header)
    print("-" * len(header))

    # Composite first
    row = f"{'COMPOSITE SCORE':<42}" + "".join(_fmt(c) for c in composite)
    for i, label in enumerate(labels):
        if i != base_idx:
            row += _delta_pct(composite[base_idx], composite[i])
    print(row)
    print()

    for name in all_subtests:
        vals = [s.get("subtests", {}).get(name) for s in scores]
        row = f"{name[:42]:<42}" + "".join(_fmt(v) for v in vals)
        for i, label in enumerate(labels):
            if i != base_idx:
                row += _delta_pct(vals[base_idx], vals[i])
        print(row)
    print()


def _print_lebench_table(
    labels: list[str], scores: list[dict[str, Any]], baseline_label: str
) -> None:
    all_syscalls: list[str] = []
    latency_maps = [s.get("latencies", {}) for s in scores]
    for m in latency_maps:
        for k in m:
            if k not in all_syscalls:
                all_syscalls.append(k)

    base_idx = labels.index(baseline_label) if baseline_label in labels else 0
    header = f"{'syscall':<24}" + "".join(f"{label[:12]:>14}" for label in labels) + "".join(
        f"  d%_vs_{labels[base_idx][:6]}" for label in labels if label != labels[base_idx]
    )
    print("LEBench Latencies (usec/call) — lower is better")
    print("=" * len(header))
    print(header)
    print("-" * len(header))

    for name in all_syscalls:
        vals = [m.get(name) for m in latency_maps]
        row = f"{name:<24}" + "".join(_fmt(v, width=14) for v in vals)
        for i, label in enumerate(labels):
            if i != base_idx:
                row += _delta_pct(vals[base_idx], vals[i])
        print(row)
    print()


def compare(label_dirs: list[tuple[str, Path]], baseline_label: str) -> dict[str, Any]:
    labels = [ld[0] for ld in label_dirs]
    run_dirs = [ld[1] for ld in label_dirs]

    btype_scores: list[tuple[str, dict[str, Any]]] = [
        _load_scores(rd) for rd in run_dirs
    ]
    btypes = [bt for bt, _ in btype_scores]
    scores = [sc for _, sc in btype_scores]

    detected = next((b for b in btypes if b not in ("unknown", "missing")), "unknown")

    print(f"\nBenchmark type detected: {detected}")
    print(f"Baseline: {baseline_label}\n")

    if detected == "unixbench":
        _print_unixbench_table(labels, scores, baseline_label)
    elif detected == "lebench":
        _print_lebench_table(labels, scores, baseline_label)
    else:
        print("No parseable benchmark output found in workload.log files.")
        print("Searched for UnixBench 'System Benchmarks Index' and LEBench 'usec/call' lines.")

    return {
        "benchmark_type": detected,
        "baseline": baseline_label,
        "runs": [
            {"label": label, "run_dir": str(rd), "scores": sc}
            for label, rd, (_, sc) in zip(labels, run_dirs, btype_scores)
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Compare UnixBench or LEBench scores across controller mode run dirs."
    )
    parser.add_argument(
        "label_dirs",
        nargs="+",
        metavar="LABEL:RUN_DIR",
        help="One or more label:run_dir pairs, e.g. noop:data/runs/run-abc",
    )
    parser.add_argument(
        "--baseline",
        default=None,
        help="Label to treat as baseline for delta %% columns (default: first label).",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Write JSON results to this path.",
    )
    args = parser.parse_args()

    label_dirs: list[tuple[str, Path]] = []
    for spec in args.label_dirs:
        if ":" not in spec:
            print(f"error: expected LABEL:RUN_DIR, got '{spec}'", file=sys.stderr)
            return 1
        label, dirstr = spec.split(":", 1)
        label_dirs.append((label, Path(dirstr)))

    baseline = args.baseline or label_dirs[0][0]
    doc = compare(label_dirs, baseline)

    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        print(f"Wrote: {args.output}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
