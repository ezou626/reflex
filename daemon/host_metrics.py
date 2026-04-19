"""Shared /proc-derived host metrics for the daemon and workload_only sampler.

Keep this module free of BCC/daemon imports so benchmarks can reuse it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

VMSTAT_RATE_KEYS = (
    "pgfault",
    "pgmajfault",
    "pswpin",
    "pswpout",
    "pgscan_direct",
    "pgscan_kswapd",
)

MEMINFO_EXTRA_KEYS = ("SReclaimable", "Active(file)", "Inactive(file)")


def parse_loadavg() -> dict[str, Any]:
    """1m/5m/15m load and optional runnable / total threads from /proc/loadavg."""
    out: dict[str, Any] = {}
    try:
        parts = Path("/proc/loadavg").read_text(encoding="utf-8").split()
    except OSError:
        return out
    if len(parts) >= 3:
        try:
            out["host_loadavg_1m"] = round(float(parts[0]), 4)
            out["host_loadavg_5m"] = round(float(parts[1]), 4)
            out["host_loadavg_15m"] = round(float(parts[2]), 4)
        except ValueError:
            pass
    if len(parts) >= 4 and "/" in parts[3]:
        left, _, right = parts[3].partition("/")
        if left.isdigit() and right.isdigit():
            out["host_loadavg_nr_running"] = int(left)
            out["host_loadavg_nr_threads"] = int(right)
    return out


def parse_proc_stat_task_counts() -> tuple[int | None, int | None]:
    """procs_running / procs_blocked from /proc/stat (Linux 2.5+)."""
    try:
        lines = Path("/proc/stat").read_text(encoding="utf-8").splitlines()
    except OSError:
        return None, None
    running: int | None = None
    blocked: int | None = None
    for line in lines:
        if line.startswith("procs_running "):
            try:
                running = int(line.split()[1])
            except (IndexError, ValueError):
                pass
        elif line.startswith("procs_blocked "):
            try:
                blocked = int(line.split()[1])
            except (IndexError, ValueError):
                pass
    return running, blocked


def parse_meminfo_extra() -> dict[str, int]:
    """Extra meminfo fields in kB (same units as MemTotal)."""
    out: dict[str, int] = {}
    want = set(MEMINFO_EXTRA_KEYS)
    try:
        for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
            if ":" not in line:
                continue
            key, rest = line.split(":", 1)
            if key not in want:
                continue
            token = rest.strip().split()[0]
            try:
                out[key] = int(token)
            except ValueError:
                continue
    except OSError:
        return {}
    return out


def meminfo_extra_to_host_features(mem: dict[str, int], mem_total: int) -> dict[str, Any]:
    """Emit host_* keys for window summaries."""
    result: dict[str, Any] = {}
    if mem_total <= 0:
        return result
    key_map = {
        "SReclaimable": "host_mem_sreclaimable_ratio",
        "Active(file)": "host_mem_active_file_ratio",
        "Inactive(file)": "host_mem_inactive_file_ratio",
    }
    for src, dst in key_map.items():
        if src in mem:
            result[dst] = round(max(0.0, min(1.0, mem[src] / mem_total)), 6)
    return result


def read_vmstat_counters(keys: tuple[str, ...] = VMSTAT_RATE_KEYS) -> dict[str, int]:
    out: dict[str, int] = {}
    try:
        for line in Path("/proc/vmstat").read_text(encoding="utf-8").splitlines():
            if " " not in line:
                continue
            k, v = line.split(None, 1)
            if k in keys:
                try:
                    out[k] = int(v.split()[0])
                except ValueError:
                    continue
    except OSError:
        return {}
    return out


def vmstat_per_sec(
    prev: dict[str, int] | None,
    cur: dict[str, int],
    dt: float,
) -> dict[str, float]:
    if prev is None or dt <= 0:
        return {}
    out: dict[str, float] = {}
    for k in VMSTAT_RATE_KEYS:
        if k in cur and k in prev:
            rate = max(0.0, (cur[k] - prev[k]) / dt)
            out[f"host_vmstat_{k}_per_sec"] = round(rate, 3)
    return out


def read_diskstats_sector_totals() -> tuple[int, int]:
    """Sum read sectors and write sectors across block devices (skip loop)."""
    read_sum = 0
    write_sum = 0
    try:
        for line in Path("/proc/diskstats").read_text(encoding="utf-8").splitlines():
            parts = line.split()
            if len(parts) < 11:
                continue
            name = parts[2]
            if name.startswith("loop"):
                continue
            try:
                read_sum += int(parts[5])
                write_sum += int(parts[9])
            except ValueError:
                continue
    except OSError:
        return 0, 0
    return read_sum, write_sum


def disk_sectors_per_sec(
    prev_r: int | None,
    prev_w: int | None,
    cur_r: int,
    cur_w: int,
    dt: float,
) -> tuple[dict[str, float], tuple[int, int]]:
    out: dict[str, float] = {}
    if prev_r is None or prev_w is None or dt <= 0:
        return out, (cur_r, cur_w)
    dr = max(0, cur_r - prev_r)
    dw = max(0, cur_w - prev_w)
    out["host_disk_read_sectors_per_sec"] = round(dr / dt, 3)
    out["host_disk_write_sectors_per_sec"] = round(dw / dt, 3)
    return out, (cur_r, cur_w)


def count_processes() -> int | None:
    try:
        return sum(1 for p in Path("/proc").iterdir() if p.name.isdigit())
    except OSError:
        return None
