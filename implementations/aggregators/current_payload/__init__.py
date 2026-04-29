from __future__ import annotations

import asyncio
import math
import struct
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_PAYLOAD_FMT = "=IIIIQiI16s"
_PAYLOAD_SIZE = struct.calcsize(_PAYLOAD_FMT)

EVENT_EXEC = 1
EVENT_FORK = 2
EVENT_EXIT = 3
EVENT_SCHED_SWITCH = 4
EVENT_SYSCALL_EXIT = 5
EVENT_RQ_LATENCY = 6
EVENT_DIRECT_RECLAIM = 7
EVENT_BLK_LATENCY = 8

EVENT_NAME = {
    EVENT_EXEC: "exec",
    EVENT_FORK: "fork",
    EVENT_EXIT: "exit",
    EVENT_SCHED_SWITCH: "sched_switch",
    EVENT_SYSCALL_EXIT: "sys_exit",
    EVENT_RQ_LATENCY: "rq_latency",
    EVENT_DIRECT_RECLAIM: "direct_reclaim",
    EVENT_BLK_LATENCY: "blk_latency",
}


@dataclass
class WindowState:
    started_ts: float
    event_counts: dict[str, int] = field(default_factory=dict)
    sys_exit_count: int = 0
    syscall_error_count: int = 0
    syscall_top_counts: dict[int, int] = field(default_factory=dict)
    rq_latency_samples_us: list[int] = field(default_factory=list)
    direct_reclaim_samples_us: list[int] = field(default_factory=list)
    blk_latency_samples_us: list[int] = field(default_factory=list)

    def add_event(self, event: dict[str, Any]) -> None:
        name = event["event_name"]
        if name == "sched_switch":
            self.event_counts["sched_switch"] = (
                self.event_counts.get("sched_switch", 0) + event.get("sw_batch", 1)
            )
            return
        self.event_counts[name] = self.event_counts.get(name, 0) + 1
        if name == "sys_exit":
            self.sys_exit_count += 1
            if int(event["ret"]) < 0:
                self.syscall_error_count += 1
            sid = int(event["syscall_id"])
            self.syscall_top_counts[sid] = self.syscall_top_counts.get(sid, 0) + 1
        elif name == "rq_latency":
            self.rq_latency_samples_us.append(int(event["rq_latency_us"]))
        elif name == "direct_reclaim":
            self.direct_reclaim_samples_us.append(int(event["reclaim_lat_us"]))
        elif name == "blk_latency":
            self.blk_latency_samples_us.append(int(event["blk_lat_us"]))


def _decode_comm(raw: bytes) -> str:
    return raw.decode("utf-8", errors="replace").rstrip("\0")


def decode_payload(chunk: bytes) -> dict[str, Any]:
    event_type, cpu, pid, tgid, ts_ns, value_i32, value_u32, comm = struct.unpack(
        _PAYLOAD_FMT, chunk
    )
    event: dict[str, Any] = {
        "record_type": "raw_event",
        "event_type": int(event_type),
        "event_name": EVENT_NAME.get(int(event_type), f"unknown_{event_type}"),
        "ts_ns": int(ts_ns),
        "cpu": int(cpu),
        "pid": int(pid),
        "tgid": int(tgid),
        "comm": _decode_comm(comm),
    }
    if event_type == EVENT_FORK:
        event["parent_pid"] = int(value_i32)
        event["child_pid"] = int(value_u32)
    elif event_type == EVENT_SCHED_SWITCH:
        event["prev_pid"] = int(value_i32)
        event["sw_batch"] = int(value_u32)  # batch count emitted by BPF (SW_BATCH=100)
    elif event_type == EVENT_SYSCALL_EXIT:
        event["syscall_id"] = int(value_u32)
        event["ret"] = int(value_i32)
        event["is_error"] = int(value_i32) < 0
    elif event_type == EVENT_RQ_LATENCY:
        event["rq_latency_us"] = int(value_u32)
    elif event_type == EVENT_DIRECT_RECLAIM:
        event["reclaim_lat_us"] = int(value_u32)
    elif event_type == EVENT_BLK_LATENCY:
        event["blk_lat_us"] = int(value_u32)
    return event


def _quantile(values: list[int], q: float) -> int:
    if not values:
        return 0
    idx = int(math.ceil((len(values) - 1) * q))
    ordered = sorted(values)
    return ordered[max(0, min(idx, len(ordered) - 1))]


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


def _host_features() -> dict[str, Any]:
    mem = _parse_meminfo()
    mem_total = mem.get("MemTotal", 0)
    out: dict[str, Any] = {}
    if mem_total > 0:
        out["host_mem_available_ratio"] = round(mem.get("MemAvailable", 0) / mem_total, 6)
    if mem.get("SwapTotal", 0) > 0:
        out["host_swap_free_ratio"] = round(mem.get("SwapFree", 0) / mem["SwapTotal"], 6)
    if "Dirty" in mem:
        out["host_dirty_kb"] = mem["Dirty"]
    try:
        load = Path("/proc/loadavg").read_text(encoding="utf-8").split()
        out["host_loadavg_1m"] = float(load[0])
    except (OSError, IndexError, ValueError):
        pass
    return out


def build_summary(window: WindowState, now_ts: float, window_sec: float) -> dict[str, Any]:
    churn_total = (
        window.event_counts.get("fork", 0)
        + window.event_counts.get("exec", 0)
        + window.event_counts.get("exit", 0)
    )
    total_sys = max(window.sys_exit_count, 1)
    top_syscalls = sorted(
        window.syscall_top_counts.items(),
        key=lambda kv: kv[1],
        reverse=True,
    )[:10]
    return {
        "record_type": "window_summary",
        "window_start_unix_s": round(window.started_ts, 6),
        "window_end_unix_s": round(now_ts, 6),
        "window_sec": window_sec,
        "metrics": {
            "process_churn_rate_per_sec": round(churn_total / window_sec, 3),
            "context_switch_rate_per_sec": round(
                window.event_counts.get("sched_switch", 0) / window_sec, 3
            ),
            "syscall_error_rate": round(window.syscall_error_count / total_sys, 6),
            "syscall_error_rate_per_sec": round(
                window.syscall_error_count / window_sec, 3
            ),
            "rq_latency_p50_us": _quantile(window.rq_latency_samples_us, 0.50),
            "rq_latency_p95_us": _quantile(window.rq_latency_samples_us, 0.95),
            "rq_latency_p99_us": _quantile(window.rq_latency_samples_us, 0.99),
            "rq_latency_count": len(window.rq_latency_samples_us),
            "direct_reclaim_rate_per_sec": round(
                len(window.direct_reclaim_samples_us) / window_sec, 3
            ),
            "direct_reclaim_lat_p95_us": _quantile(window.direct_reclaim_samples_us, 0.95),
            "blk_latency_p50_us": _quantile(window.blk_latency_samples_us, 0.50),
            "blk_latency_p95_us": _quantile(window.blk_latency_samples_us, 0.95),
            "blk_latency_count": len(window.blk_latency_samples_us),
        },
        "event_counts": dict(window.event_counts),
        "top_syscalls": [
            {"syscall_id": sid, "count": count} for sid, count in top_syscalls
        ],
        "host_features": _host_features(),
    }


class CurrentPayloadAggregator:
    def __init__(
        self,
        loader_cmd: Sequence[str],
        *,
        window_sec: float = 1.0,
        trigger_reason: str = "timer_window",
    ) -> None:
        self.loader_cmd = list(loader_cmd)
        self.window_sec = window_sec
        self.trigger_reason = trigger_reason
        self.process: asyncio.subprocess.Process | None = None
        self.window = WindowState(started_ts=time.time())

    async def setup(self, runtime: Any) -> None:
        self.process = await asyncio.create_subprocess_exec(
            *self.loader_cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
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
                chunk = await self.process.stdout.readexactly(_PAYLOAD_SIZE)
            except asyncio.IncompleteReadError:
                await runtime.log_event(
                    "aggregator_stop",
                    "loader stream ended",
                    {"loader_cmd": self.loader_cmd},
                )
                await runtime.stop()
                return
            self.window.add_event(decode_payload(chunk))
            now = time.time()
            if now - self.window.started_ts >= self.window_sec:
                summary = build_summary(self.window, now, self.window_sec)
                await runtime.accept_sample(summary)
                await runtime.trigger_controller(
                    self.trigger_reason,
                    {"window_start_unix_s": summary["window_start_unix_s"]},
                )
                self.window = WindowState(started_ts=now)

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
