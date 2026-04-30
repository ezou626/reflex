#!/usr/bin/env python3
"""Parsers for UnixBench, LEBench, and sysbench workload.log output."""
from __future__ import annotations

import re
from typing import Any


# ---------------------------------------------------------------------------
# UnixBench (byte-unixbench)
# ---------------------------------------------------------------------------

def parse_unixbench(text: str) -> dict[str, Any]:
    """
    Extract composite score and per-subtest index values from UnixBench output.

    The Run script emits lines like:
        System Benchmarks Index Score                              1234.5
    and per-subtest rows in the index table like:
        Dhrystone 2 using register variables     1000000.0   34872756.5    34872.8
    """
    result: dict[str, Any] = {"composite_score": None, "subtests": {}}

    composite_re = re.compile(
        r"System Benchmarks Index Score\s*(?:\(Partial Only\))?\s+([\d.]+)"
    )
    # index-table row: name  baseline  result  index
    subtest_re = re.compile(
        r"^(.+?)\s{2,}([\d.]+)\s+([\d.]+)\s+([\d.]+)\s*$"
    )

    in_index_table = False
    for line in text.splitlines():
        m = composite_re.search(line)
        if m:
            result["composite_score"] = float(m.group(1))
            continue

        if "System Benchmarks Index Values" in line:
            in_index_table = True
            continue

        if in_index_table:
            m2 = subtest_re.match(line.rstrip())
            if m2:
                name = m2.group(1).strip()
                index = float(m2.group(4))
                result["subtests"][name] = index

    return result


# ---------------------------------------------------------------------------
# sysbench
# ---------------------------------------------------------------------------

def parse_sysbench(text: str) -> dict[str, Any]:
    """
    Extract common metrics from sysbench output.

    sysbench reports different throughput labels by test. CPU/thread/mutex/fileio
    workloads commonly emit "events per second"; memory emits a transferred line
    with MiB/sec in parentheses. All standard tests include total time and latency
    percentiles.
    """
    result: dict[str, Any] = {
        "test": None,
        "events_per_sec": None,
        "total_events": None,
        "total_time_sec": None,
        "latency_ms": {},
        "memory_mib_per_sec": None,
        "primary_metric": None,
        "primary_value": None,
    }

    generic_test_re = re.compile(
        r"^Running the test with following options:",
        re.MULTILINE,
    )
    memory_test_re = re.compile(r"^Running memory speed test", re.MULTILINE)
    events_re = re.compile(r"events per second:\s+([\d.]+)")
    total_events_re = re.compile(r"total number of events:\s+(\d+)")
    total_time_re = re.compile(r"total time:\s+([\d.]+)s")
    memory_re = re.compile(r"\(([\d.]+)\s+MiB/sec\)")
    latency_re = re.compile(
        r"^\s*(min|avg|max|95th percentile|sum):\s+([\d.]+)\s*$"
    )
    command_re = re.compile(r"^\+\s+\S*sysbench\s+([\w_-]+)\b", re.MULTILINE)

    test_match = command_re.search(text)
    if test_match:
        result["test"] = test_match.group(1)
    elif memory_test_re.search(text):
        result["test"] = "memory"
    elif generic_test_re.search(text):
        result["test"] = "unknown"

    for line in text.splitlines():
        if m := events_re.search(line):
            result["events_per_sec"] = float(m.group(1))
        elif m := total_events_re.search(line):
            result["total_events"] = int(m.group(1))
        elif m := total_time_re.search(line):
            result["total_time_sec"] = float(m.group(1))
        elif "transferred" in line:
            if m := memory_re.search(line):
                result["memory_mib_per_sec"] = float(m.group(1))
        elif m := latency_re.match(line):
            name = m.group(1).replace(" ", "_")
            result["latency_ms"][name] = float(m.group(2))

    if result["memory_mib_per_sec"] is not None:
        result["primary_metric"] = "memory_mib_per_sec"
        result["primary_value"] = result["memory_mib_per_sec"]
    elif result["events_per_sec"] is not None:
        result["primary_metric"] = "events_per_sec"
        result["primary_value"] = result["events_per_sec"]
    elif result["total_events"] is not None and result["total_time_sec"]:
        result["primary_metric"] = "events_per_sec"
        result["primary_value"] = result["total_events"] / result["total_time_sec"]

    return result


# ---------------------------------------------------------------------------
# LEBench
# ---------------------------------------------------------------------------

def parse_lebench(text: str) -> dict[str, float]:
    """
    Extract per-syscall latencies from LEBench output.

    Lines look like:
        getpid:    0.123 usec/call
        fork+exec: 1700.0 usec/call
    """
    result: dict[str, float] = {}
    row_re = re.compile(r"^([\w+/]+):\s+([\d.]+)\s+usec/call")
    for line in text.splitlines():
        m = row_re.match(line.strip())
        if m:
            result[m.group(1)] = float(m.group(2))
    return result


# ---------------------------------------------------------------------------
# Auto-detect and parse
# ---------------------------------------------------------------------------

def detect_and_parse(text: str) -> tuple[str, dict[str, Any]]:
    """
    Returns (benchmark_type, parsed_scores) where benchmark_type is
    'unixbench', 'sysbench', 'lebench', or 'unknown'.
    """
    if "System Benchmarks Index" in text or "Dhrystone" in text:
        return "unixbench", parse_unixbench(text)
    if "sysbench " in text and "General statistics:" in text:
        return "sysbench", parse_sysbench(text)
    if re.search(r"^\w[\w+/]*:\s+[\d.]+\s+usec/call", text, re.MULTILINE):
        return "lebench", {"latencies": parse_lebench(text)}
    return "unknown", {}


if __name__ == "__main__":
    import sys
    text = sys.stdin.read()
    btype, scores = detect_and_parse(text)
    import json
    print(json.dumps({"type": btype, "scores": scores}, indent=2))
