#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import signal
import sys
import time
from pathlib import Path
from typing import Any

_REPO = Path(__file__).resolve().parents[1]
_DAEMON = _REPO / "daemon"
if str(_DAEMON) not in sys.path:
    sys.path.insert(0, str(_DAEMON))
import host_metrics  # noqa: E402


def _parse_proc_stat() -> tuple[int, int] | None:
    try:
        lines = Path("/proc/stat").read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    line = next((x for x in lines if x.startswith("cpu ")), None)
    if line is None:
        return None
    parts = line.split()
    if len(parts) < 5:
        return None
    vals = [int(v) for v in parts[1:] if v.isdigit()]
    if not vals:
        return None
    total = sum(vals)
    idle = int(parts[4])
    return total, idle


def _mem_available_ratio() -> float | None:
    mem_total = None
    mem_avail = None
    try:
        for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
            if line.startswith("MemTotal:"):
                mem_total = int(line.split()[1])
            elif line.startswith("MemAvailable:"):
                mem_avail = int(line.split()[1])
    except OSError:
        return None
    if not mem_total or mem_avail is None:
        return None
    return max(0.0, min(1.0, mem_avail / mem_total))


def _mem_total_kb() -> int:
    try:
        for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
            if line.startswith("MemTotal:"):
                return int(line.split()[1])
    except OSError:
        return 0
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Sample host metrics into summary JSONL.")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--window-sec", type=float, default=1.0)
    args = parser.parse_args()

    running = True

    def _stop(_sig: int, _frame: Any) -> None:
        nonlocal running
        running = False

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    prev = _parse_proc_stat()
    prev_t = time.time()
    rate_ref_ts: float | None = None
    prev_vmstat: dict[str, int] | None = None
    prev_disk_r: int | None = None
    prev_disk_w: int | None = None
    args.output.parent.mkdir(parents=True, exist_ok=True)

    with args.output.open("a", encoding="utf-8", buffering=1) as out:
        while running:
            time.sleep(max(args.window_sec, 0.2))
            now = time.time()
            ref_ts = rate_ref_ts
            stat = _parse_proc_stat()
            host: dict[str, Any] = {}
            if prev is not None and stat is not None:
                d_total = max(stat[0] - prev[0], 0)
                d_idle = max(stat[1] - prev[1], 0)
                busy = 1.0 - (d_idle / d_total) if d_total > 0 else 0.0
                host["host_cpu_busy_ratio"] = round(max(0.0, min(1.0, busy)), 6)
            mem_ratio = _mem_available_ratio()
            if mem_ratio is not None:
                host["host_mem_available_ratio"] = round(mem_ratio, 6)
            mem_total = _mem_total_kb()
            mem_extra = host_metrics.parse_meminfo_extra()
            host.update(host_metrics.meminfo_extra_to_host_features(mem_extra, mem_total))

            pr, pb = host_metrics.parse_proc_stat_task_counts()
            if pr is not None:
                host["host_procs_running"] = pr
            if pb is not None:
                host["host_procs_blocked"] = pb

            host.update(host_metrics.parse_loadavg())

            pc = host_metrics.count_processes()
            if pc is not None:
                host["host_process_count"] = pc

            cur_vm = host_metrics.read_vmstat_counters()
            if cur_vm and ref_ts is not None and prev_vmstat is not None:
                dt = max(now - ref_ts, 1e-6)
                host.update(host_metrics.vmstat_per_sec(prev_vmstat, cur_vm, dt))
            if cur_vm:
                prev_vmstat = cur_vm

            cur_dr, cur_dw = host_metrics.read_diskstats_sector_totals()
            if ref_ts is not None and prev_disk_r is not None and prev_disk_w is not None:
                dt = max(now - ref_ts, 1e-6)
                rates, _ = host_metrics.disk_sectors_per_sec(prev_disk_r, prev_disk_w, cur_dr, cur_dw, dt)
                host.update(rates)
            prev_disk_r = cur_dr
            prev_disk_w = cur_dw

            rate_ref_ts = now

            rec = {
                "record_type": "window_summary",
                "window_start_unix_s": round(prev_t, 6),
                "window_end_unix_s": round(now, 6),
                "window_sec": round(now - prev_t, 6),
                "feature_namespace": "workload_only",
                "metrics": {k: v for k, v in host.items() if isinstance(v, (int, float))},
                "host_features": host,
                "event_counts": {},
                "top_syscalls": [],
            }
            out.write(json.dumps(rec, ensure_ascii=False) + "\n")
            prev = stat
            prev_t = now

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
