from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DAEMON = REPO_ROOT / "daemon"
if str(DAEMON) not in sys.path:
    sys.path.insert(0, str(DAEMON))

import host_metrics  # noqa: E402


def test_parse_loadavg_has_1m_5m_15m() -> None:
    d = host_metrics.parse_loadavg()
    assert "host_loadavg_1m" in d
    assert "host_loadavg_5m" in d
    assert "host_loadavg_15m" in d


def test_read_vmstat_counters_non_empty() -> None:
    m = host_metrics.read_vmstat_counters()
    assert "pgfault" in m
    assert m["pgfault"] >= 0


def test_vmstat_per_sec_positive_delta() -> None:
    prev = {"pgfault": 100, "pgmajfault": 1, "pswpin": 0, "pswpout": 0, "pgscan_direct": 0, "pgscan_kswapd": 0}
    cur = {k: prev[k] + 50 for k in prev}
    rates = host_metrics.vmstat_per_sec(prev, cur, 1.0)
    assert rates["host_vmstat_pgfault_per_sec"] == 50.0


def test_count_processes_reasonable() -> None:
    n = host_metrics.count_processes()
    assert n is not None
    assert n > 10
