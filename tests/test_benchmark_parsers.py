from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "benchmarks"))

from parse_benchmark_scores import detect_and_parse, parse_sysbench  # noqa: E402


def test_parse_sysbench_cpu_output() -> None:
    text = """
sysbench 1.0.20 (using system LuaJIT 2.1.0-beta3)

Running the test with following options:
Number of threads: 4

General statistics:
    total time:                          10.0001s
    total number of events:              12345

Latency (ms):
         min:                                    0.10
         avg:                                    0.20
         max:                                    4.00
         95th percentile:                        0.35
         sum:                                 2469.00

Threads fairness:
    events (avg/stddev):           3086.2500/10.00
    execution time (avg/stddev):   2.5000/0.01

events per second: 1234.56
"""

    parsed = parse_sysbench(text)

    assert parsed["events_per_sec"] == 1234.56
    assert parsed["total_events"] == 12345
    assert parsed["total_time_sec"] == 10.0001
    assert parsed["latency_ms"]["95th_percentile"] == 0.35
    assert parsed["primary_metric"] == "events_per_sec"
    assert parsed["primary_value"] == 1234.56


def test_parse_sysbench_memory_output() -> None:
    text = """
sysbench 1.0.20 (using system LuaJIT 2.1.0-beta3)

Running memory speed test with the following options:
Total operations: 10240 (2048.00 per second)

10240.00 MiB transferred (2048.00 MiB/sec)

General statistics:
    total time:                          5.0001s
    total number of events:              10240
"""

    btype, parsed = detect_and_parse(text)

    assert btype == "sysbench"
    assert parsed["memory_mib_per_sec"] == 2048.0
    assert parsed["primary_metric"] == "memory_mib_per_sec"
    assert parsed["primary_value"] == 2048.0
