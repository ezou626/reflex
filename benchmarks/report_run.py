#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import statistics
from collections import Counter
from pathlib import Path
from typing import Any


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    records: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            records.append(json.loads(line))
    return records


def _extract_numeric(records: list[dict[str, Any]], key: str) -> list[float]:
    out: list[float] = []
    for rec in records:
        metrics = rec.get("metrics", {})
        host = rec.get("host_features", {})
        if key in metrics and isinstance(metrics[key], (int, float)):
            out.append(float(metrics[key]))
        elif key in host and isinstance(host[key], (int, float)):
            out.append(float(host[key]))
    return out


def _basic_stats(values: list[float]) -> dict[str, float] | None:
    if not values:
        return None
    mean = statistics.fmean(values)
    p50 = statistics.median(values)
    p95 = _percentile(values, 0.95)
    return {
        "count": float(len(values)),
        "mean": round(mean, 6),
        "min": round(min(values), 6),
        "max": round(max(values), 6),
        "p50": round(p50, 6),
        "p95": round(p95, 6),
    }


def _percentile(values: list[float], q: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    idx = int((len(ordered) - 1) * q)
    return ordered[idx]


def build_report(run_dir: Path) -> dict[str, Any]:
    events = _read_jsonl(run_dir / "events.jsonl")
    summaries = _read_jsonl(run_dir / "summary.jsonl")
    decisions = _read_jsonl(run_dir / "decisions.jsonl")

    event_dist = Counter(rec.get("event_name", "unknown") for rec in events)
    decision_dist = Counter(rec.get("record_type", "unknown") for rec in decisions)
    trigger_dist = Counter(
        rec.get("trigger", "none")
        for rec in decisions
        if rec.get("record_type") == "decision"
    )
    decision_reason_dist = Counter(
        rec.get("reason", "none")
        for rec in decisions
        if rec.get("record_type") == "decision"
    )

    tracked_metrics = [
        "process_churn_rate_per_sec",
        "context_switch_rate_per_sec",
        "syscall_error_rate",
        "rq_latency_p95_us",
        "rq_latency_p99_us",
        "host_cpu_busy_ratio",
        "host_mem_available_ratio",
    ]
    metric_stats = {
        key: _basic_stats(_extract_numeric(summaries, key)) for key in tracked_metrics
    }

    return {
        "run_dir": str(run_dir),
        "files": {
            "events_jsonl": str(run_dir / "events.jsonl"),
            "summary_jsonl": str(run_dir / "summary.jsonl"),
            "decisions_jsonl": str(run_dir / "decisions.jsonl"),
            "daemon_log": str(run_dir / "daemon.log"),
            "workload_log": str(run_dir / "workload.log"),
        },
        "counts": {
            "events": len(events),
            "summary_windows": len(summaries),
            "decision_records": len(decisions),
        },
        "event_distribution": dict(event_dist),
        "decision_record_distribution": dict(decision_dist),
        "decision_trigger_distribution": dict(trigger_dist),
        "decision_reason_distribution": dict(decision_reason_dist),
        "metric_stats": {k: v for k, v in metric_stats.items() if v is not None},
    }


def _to_markdown(report: dict[str, Any]) -> str:
    lines: list[str] = []
    lines.append("# Reflex Run Report")
    lines.append("")
    lines.append(f"- Run directory: `{report['run_dir']}`")
    counts = report["counts"]
    lines.append(
        "- Counts: "
        f"events={counts['events']}, "
        f"summary_windows={counts['summary_windows']}, "
        f"decision_records={counts['decision_records']}"
    )
    lines.append("")

    lines.append("## Event Distribution")
    if report["event_distribution"]:
        for key, value in sorted(report["event_distribution"].items()):
            lines.append(f"- `{key}`: {value}")
    else:
        lines.append("- No events found.")
    lines.append("")

    lines.append("## Decision Stats")
    for title, key in (
        ("Record Types", "decision_record_distribution"),
        ("Triggers", "decision_trigger_distribution"),
        ("Reasons", "decision_reason_distribution"),
    ):
        lines.append(f"### {title}")
        data = report[key]
        if data:
            for item_key, value in sorted(data.items()):
                lines.append(f"- `{item_key}`: {value}")
        else:
            lines.append("- No records found.")
        lines.append("")

    lines.append("## Metric Stats")
    metrics = report["metric_stats"]
    if not metrics:
        lines.append("- No summary metrics found.")
        return "\n".join(lines)
    for key, stat in sorted(metrics.items()):
        lines.append(
            f"- `{key}`: mean={stat['mean']}, p50={stat['p50']}, p95={stat['p95']}, "
            f"min={stat['min']}, max={stat['max']}, n={int(stat['count'])}"
        )
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Build a report for a Reflex run directory.")
    parser.add_argument("run_dir", type=Path, help="Path to data/runs/<run_id>")
    parser.add_argument(
        "--format",
        choices=("json", "markdown"),
        default="markdown",
        help="Output format.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Optional output file path. Prints to stdout when omitted.",
    )
    args = parser.parse_args()

    report = build_report(args.run_dir)
    if args.format == "json":
        rendered = json.dumps(report, indent=2)
    else:
        rendered = _to_markdown(report)

    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n", encoding="utf-8")
    else:
        print(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
