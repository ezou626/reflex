from __future__ import annotations

import asyncio
import os
import struct
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

_SUMMARY_FMT = "=QIIIIIIIIIII"
_SUMMARY_SIZE = struct.calcsize(_SUMMARY_FMT)


def _parse_meminfo() -> dict[str, int]:
    keys = {"MemTotal", "MemAvailable", "SwapTotal", "SwapFree", "Dirty"}
    out: dict[str, int] = {}
    try:
        for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
            if ":" not in line:
                continue
            key, rest = line.split(":", 1)
            if key in keys:
                out[key] = int(rest.strip().split()[0])
    except OSError:
        return {}
    return out


def _parse_cpu_stat(
    previous: dict[str, int] | None = None,
) -> tuple[dict[str, Any], dict[str, int] | None]:
    out: dict[str, Any] = {}
    snapshot: dict[str, int] | None = None
    try:
        lines = Path("/proc/stat").read_text(encoding="utf-8").splitlines()
        cpu_count = 0
        for line in lines:
            if line.startswith("cpu"):
                cpu_count += 1
            else:
                break
        cpu_count -= 1
        out["host_cpu_count"] = max(1, cpu_count)

        cpu_line = lines[0]
        if cpu_line.startswith("cpu "):
            parts = cpu_line.split()
            if len(parts) >= 9:
                user = int(parts[1])
                nice = int(parts[2])
                system = int(parts[3])
                idle = int(parts[4])
                iowait = int(parts[5])
                steal = int(parts[8]) if len(parts) > 8 else 0

                total = user + nice + system + idle + iowait + steal
                snapshot = {
                    "user": user,
                    "nice": nice,
                    "system": system,
                    "idle": idle,
                    "iowait": iowait,
                    "steal": steal,
                    "total": total,
                }
                if previous is not None:
                    delta_total = max(total - previous.get("total", total), 0)
                    if delta_total > 0:
                        delta_user_nice = max(
                            (user + nice)
                            - (previous.get("user", user) + previous.get("nice", nice)),
                            0,
                        )
                        delta_system = max(system - previous.get("system", system), 0)
                        delta_iowait = max(iowait - previous.get("iowait", iowait), 0)
                        delta_steal = max(steal - previous.get("steal", steal), 0)
                        delta_idle = max(idle - previous.get("idle", idle), 0)
                        out["host_cpu_user_pct"] = round(
                            100.0 * delta_user_nice / delta_total,
                            2,
                        )
                        out["host_cpu_system_pct"] = round(
                            100.0 * delta_system / delta_total,
                            2,
                        )
                        out["host_cpu_iowait_pct"] = round(
                            100.0 * delta_iowait / delta_total,
                            2,
                        )
                        out["host_cpu_steal_pct"] = round(
                            100.0 * delta_steal / delta_total,
                            2,
                        )
                        out["host_cpu_idle_pct"] = round(
                            100.0 * delta_idle / delta_total,
                            2,
                        )
                        out["host_cpu_util_pct"] = round(
                            100.0 * (1.0 - delta_idle / delta_total),
                            2,
                        )
    except (OSError, IndexError, ValueError):
        return out, snapshot
    return out, snapshot


def _host_features(previous_cpu_stat: dict[str, int] | None = None) -> tuple[dict[str, Any], dict[str, int] | None]:
    mem = _parse_meminfo()
    mem_total = mem.get("MemTotal", 0)
    cpu_stats, cpu_snapshot = _parse_cpu_stat(previous_cpu_stat)
    out: dict[str, Any] = {}
    if mem_total > 0:
        out["host_mem_available_ratio"] = round(mem.get("MemAvailable", 0) / mem_total, 6)
    if mem.get("SwapTotal", 0) > 0:
        out["host_swap_free_ratio"] = round(mem.get("SwapFree", 0) / mem["SwapTotal"], 6)
    if "Dirty" in mem:
        out["host_dirty_kb"] = mem["Dirty"]
    out.update(cpu_stats)
    try:
        load = Path("/proc/loadavg").read_text(encoding="utf-8").split()
        out["host_loadavg_1m"] = float(load[0])
        cpu_count = cpu_stats.get("host_cpu_count", 1)
        if len(load) >= 1 and cpu_count > 0:
            out["host_load_per_cpu"] = round(float(load[0]) / cpu_count, 3)
    except (OSError, IndexError, ValueError):
        pass
    return out, cpu_snapshot


def decode_summary(
    chunk: bytes,
    *,
    window_sec: float,
    received_ts: float | None = None,
    host_features: dict[str, Any] | None = None,
) -> dict[str, Any]:
    (
        window_end_ns,
        rq_p95_us,
        rq_latency_count,
        syscall_count,
        failure_count,
        syscall_p95_us,
        blk_p95_us,
        blk_latency_count,
        ctx_switch_count,
        direct_reclaim_count,
        direct_reclaim_p95_us,
        fork_count,
    ) = struct.unpack(_SUMMARY_FMT, chunk)
    now_ts = time.time() if received_ts is None else received_ts
    total_sys = max(int(syscall_count), 1)
    return {
        "record_type": "window_summary",
        "window_start_unix_s": round(now_ts - window_sec, 6),
        "window_end_unix_s": round(now_ts, 6),
        "loader_window_end_ns": int(window_end_ns),
        "window_sec": window_sec,
        "metrics": {
            "process_churn_rate_per_sec": round(int(fork_count) / window_sec, 3),
            "context_switch_rate_per_sec": round(int(ctx_switch_count) / window_sec, 3),
            "syscall_error_rate": round(int(failure_count) / total_sys, 6),
            "syscall_error_rate_per_sec": round(int(failure_count) / window_sec, 3),
            "syscall_latency_p95_us": int(syscall_p95_us),
            "syscall_latency_count": int(syscall_count),
            "rq_latency_p95_us": int(rq_p95_us),
            "rq_latency_count": int(rq_latency_count),
            "direct_reclaim_rate_per_sec": round(int(direct_reclaim_count) / window_sec, 3),
            "direct_reclaim_lat_p95_us": int(direct_reclaim_p95_us),
            "blk_latency_p95_us": int(blk_p95_us),
            "blk_latency_count": int(blk_latency_count),
        },
        "event_counts": {
            "fork": int(fork_count),
            "sched_switch": int(ctx_switch_count),
            "sys_exit": int(syscall_count),
            "direct_reclaim": int(direct_reclaim_count),
        },
        "top_syscalls": [],
        "host_features": host_features if host_features is not None else _host_features()[0],
    }


class WindowSummaryAggregator:
    def __init__(
        self,
        loader_cmd: Sequence[str],
        *,
        window_sec: float = 1.0,
        trigger_on_sample: bool = True,
    ) -> None:
        self.loader_cmd = list(loader_cmd)
        self.window_sec = window_sec
        self.trigger_on_sample = trigger_on_sample
        self.process: asyncio.subprocess.Process | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._prev_cpu_stat: dict[str, int] | None = None

    async def setup(self, runtime: Any) -> None:
        env = os.environ.copy()
        env["REFLEX_WINDOW_SEC"] = str(self.window_sec)
        self.process = await asyncio.create_subprocess_exec(
            *self.loader_cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        if self.process.stderr is not None:
            self._stderr_task = asyncio.create_task(self._drain_stderr(self.process.stderr))
        await runtime.log_event(
            "aggregator_setup",
            "current payload loader started",
            {"loader_cmd": self.loader_cmd},
        )

    async def run(self, runtime: Any) -> None:
        if self.process is None or self.process.stdout is None:
            raise RuntimeError("aggregator setup did not start loader process")
        while True:
            try:
                chunk = await self.process.stdout.readexactly(_SUMMARY_SIZE)
            except asyncio.IncompleteReadError:
                await runtime.log_event(
                    "aggregator_stop",
                    "loader stream ended",
                    {"loader_cmd": self.loader_cmd},
                )
                await runtime.stop()
                return
            host_features, self._prev_cpu_stat = _host_features(self._prev_cpu_stat)
            summary = decode_summary(
                chunk,
                window_sec=self.window_sec,
                host_features=host_features,
            )
            await runtime.accept_sample(summary)
            if self.trigger_on_sample:
                await runtime.trigger_controller(
                    "trigger on sample delivery",
                    {"window_start_unix_s": summary["window_start_unix_s"]},
                )

    async def stop(self) -> None:
        if self.process is None:
            return
        if self.process.returncode is None:
            self.process.terminate()
            try:
                await asyncio.wait_for(self.process.wait(), timeout=2.0)
            except TimeoutError:
                self.process.kill()
                await self.process.wait()
        if self._stderr_task is not None:
            self._stderr_task.cancel()
            await asyncio.gather(self._stderr_task, return_exceptions=True)

    @staticmethod
    async def _drain_stderr(stderr: asyncio.StreamReader) -> None:
        while True:
            chunk = await stderr.read(4096)
            if not chunk:
                return


__all__ = ["WindowSummaryAggregator", "decode_summary"]
