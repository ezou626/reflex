#!/usr/bin/env python3
"""Parsers for UnixBench and LEBench workload.log output."""
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
    'unixbench', 'lebench', or 'unknown'.
    """
    if "System Benchmarks Index" in text or "Dhrystone" in text:
        return "unixbench", parse_unixbench(text)
    if re.search(r"^\w[\w+/]*:\s+[\d.]+\s+usec/call", text, re.MULTILINE):
        return "lebench", {"latencies": parse_lebench(text)}
    return "unknown", {}


if __name__ == "__main__":
    import sys
    text = sys.stdin.read()
    btype, scores = detect_and_parse(text)
    import json
    print(json.dumps({"type": btype, "scores": scores}, indent=2))
