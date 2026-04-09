#!/usr/bin/env python3
"""Reflex userspace daemon for Phase-1 telemetry extraction.

This daemon consumes BPF ring-buffer events and emits:
1) raw events JSONL
2) window summaries JSONL (rates/latencies + host /proc-/sys features)

Phase-1 eBPF metrics:
- process churn (fork/exec/exit)
- context switch rate
- syscall error rate
- run-queue wakeup->oncpu latency
"""

from __future__ import annotations

import argparse
import json
import math
import signal
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

EVENT_EXEC = 1
EVENT_FORK = 2
EVENT_EXIT = 3
EVENT_SCHED_SWITCH = 4
EVENT_SYSCALL_EXIT = 5
EVENT_RQ_LATENCY = 6

EVENT_NAME = {
    EVENT_EXEC: "exec",
    EVENT_FORK: "fork",
    EVENT_EXIT: "exit",
    EVENT_SCHED_SWITCH: "sched_switch",
    EVENT_SYSCALL_EXIT: "sys_exit",
    EVENT_RQ_LATENCY: "rq_latency",
}

PHASE1_METRIC_CONTRACTS: dict[str, dict[str, Any]] = {
    "process_churn_rate_per_sec": {
        "definition": "fork+exec+exit events per second",
        "unit": "events/s",
        "window": "1s",
        "expected_range": [0, 100000],
    },
    "context_switch_rate_per_sec": {
        "definition": "sched_switch events per second",
        "unit": "switches/s",
        "window": "1s",
        "expected_range": [0, 1000000],
    },
    "syscall_error_rate": {
        "definition": "sys_exit(ret<0) / all sys_exit",
        "unit": "ratio",
        "window": "1s",
        "expected_range": [0.0, 1.0],
    },
    "rq_latency_us": {
        "definition": "wakeup->oncpu latency microseconds",
        "unit": "us",
        "window": "1s",
        "expected_range": [0, 10000000],
    },
}


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _decode_comm(raw: Any) -> str:
    return raw.decode("utf-8", errors="replace").rstrip("\0")


def _read_first_token(path: str, default: float | None = None) -> float | None:
    try:
        text = Path(path).read_text(encoding="utf-8").strip()
    except OSError:
        return default
    if not text:
        return default
    token = text.split()[0]
    try:
        return float(token)
    except ValueError:
        return default


def _parse_proc_stat() -> tuple[int, int, int] | None:
    """Return (total_jiffies, idle_jiffies, ctxt_total)."""
    try:
        lines = Path("/proc/stat").read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    cpu_line = next((ln for ln in lines if ln.startswith("cpu ")), None)
    ctxt_line = next((ln for ln in lines if ln.startswith("ctxt ")), None)
    if cpu_line is None or ctxt_line is None:
        return None
    parts = cpu_line.split()
    if len(parts) < 5:
        return None
    values = [int(v) for v in parts[1:] if v.isdigit()]
    if not values:
        return None
    total = sum(values)
    idle = int(parts[4])
    ctxt = int(ctxt_line.split()[1])
    return total, idle, ctxt


def _parse_meminfo() -> dict[str, int]:
    keys = {"MemTotal", "MemAvailable", "SwapTotal", "SwapFree", "Dirty"}
    out: dict[str, int] = {}
    try:
        for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
            if ":" not in line:
                continue
            key, rest = line.split(":", 1)
            if key not in keys:
                continue
            token = rest.strip().split()[0]
            out[key] = int(token)
    except OSError:
        return {}
    return out


def _parse_psi(resource: str) -> dict[str, float]:
    path = Path(f"/proc/pressure/{resource}")
    if not path.exists():
        return {}
    out: dict[str, float] = {}
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            parts = line.split()
            if len(parts) < 2:
                continue
            prefix = parts[0]
            for field in parts[1:]:
                if "=" not in field:
                    continue
                k, v = field.split("=", 1)
                try:
                    out[f"{resource}_{prefix}_{k}"] = float(v)
                except ValueError:
                    continue
    except OSError:
        return {}
    return out


@dataclass
class WindowState:
    """Mutable 1-second window aggregations."""

    started_ts: float
    event_counts: dict[str, int] = field(default_factory=dict)
    sys_exit_count: int = 0
    syscall_error_count: int = 0
    syscall_top_counts: dict[int, int] = field(default_factory=dict)
    rq_latency_samples_us: list[int] = field(default_factory=list)

    def add_event(self, event: dict[str, Any]) -> None:
        name = event["event_name"]
        self.event_counts[name] = self.event_counts.get(name, 0) + 1
        if name == "sys_exit":
            self.sys_exit_count += 1
            if int(event["ret"]) < 0:
                self.syscall_error_count += 1
            sid = int(event["syscall_id"])
            self.syscall_top_counts[sid] = self.syscall_top_counts.get(sid, 0) + 1
        elif name == "rq_latency":
            self.rq_latency_samples_us.append(int(event["rq_latency_us"]))


@dataclass
class ProcSampleState:
    prev_total_jiffies: int | None = None
    prev_idle_jiffies: int | None = None
    prev_ctxt: int | None = None
    prev_sample_ts: float | None = None

    def sample(self, now_ts: float) -> dict[str, Any]:
        result: dict[str, Any] = {}
        stat = _parse_proc_stat()
        if stat is not None:
            total, idle, ctxt = stat
            if (
                self.prev_total_jiffies is not None
                and self.prev_idle_jiffies is not None
                and self.prev_ctxt is not None
                and self.prev_sample_ts is not None
            ):
                dt = max(now_ts - self.prev_sample_ts, 1e-6)
                d_total = max(total - self.prev_total_jiffies, 0)
                d_idle = max(idle - self.prev_idle_jiffies, 0)
                d_ctxt = max(ctxt - self.prev_ctxt, 0)
                busy = 1.0 - (d_idle / d_total) if d_total > 0 else 0.0
                result["host_cpu_busy_ratio"] = round(max(0.0, min(1.0, busy)), 6)
                result["host_ctxt_rate_per_sec"] = round(d_ctxt / dt, 3)
            self.prev_total_jiffies = total
            self.prev_idle_jiffies = idle
            self.prev_ctxt = ctxt
            self.prev_sample_ts = now_ts

        mem = _parse_meminfo()
        mem_total = mem.get("MemTotal", 0)
        mem_avail = mem.get("MemAvailable", 0)
        if mem_total > 0:
            result["host_mem_available_ratio"] = round(mem_avail / mem_total, 6)
        if mem.get("SwapTotal", 0) > 0:
            result["host_swap_free_ratio"] = round(
                mem.get("SwapFree", 0) / mem["SwapTotal"], 6
            )
        if "Dirty" in mem:
            result["host_dirty_kb"] = mem["Dirty"]

        la = _read_first_token("/proc/loadavg")
        if la is not None:
            result["host_loadavg_1m"] = round(la, 4)

        for resource in ("cpu", "memory", "io"):
            result.update(_parse_psi(resource))
        return result


def _quantile(values: list[int], q: float) -> int:
    if not values:
        return 0
    idx = int(math.ceil((len(values) - 1) * q))
    ordered = sorted(values)
    return ordered[max(0, min(idx, len(ordered) - 1))]


def _to_event_dict(ev: Any) -> dict[str, Any]:
    event_type = int(ev.event_type)
    base = {
        "record_type": "raw_event",
        "event_type": event_type,
        "event_name": EVENT_NAME.get(event_type, f"unknown_{event_type}"),
        "ts_ns": int(ev.ts_ns),
        "cpu": int(ev.cpu),
        "pid": int(ev.pid),
        "tgid": int(ev.tgid),
        "comm": _decode_comm(ev.comm),
    }
    if event_type == EVENT_FORK:
        base["parent_pid"] = int(ev.value_i32)
        base["child_pid"] = int(ev.value_u32)
    elif event_type == EVENT_SCHED_SWITCH:
        base["prev_pid"] = int(ev.value_i32)
        base["next_pid"] = int(ev.value_u32)
    elif event_type == EVENT_SYSCALL_EXIT:
        base["syscall_id"] = int(ev.value_u32)
        base["ret"] = int(ev.value_i32)
        base["is_error"] = int(ev.value_i32) < 0
    elif event_type == EVENT_RQ_LATENCY:
        base["rq_latency_us"] = int(ev.value_u32)
    return base


def _build_summary(
    window: WindowState,
    host: dict[str, Any],
    now_ts: float,
    window_sec: float,
) -> dict[str, Any]:
    churn_total = (
        window.event_counts.get("fork", 0)
        + window.event_counts.get("exec", 0)
        + window.event_counts.get("exit", 0)
    )
    total_sys = max(window.sys_exit_count, 1)
    err_ratio = window.syscall_error_count / total_sys
    top_syscalls = sorted(
        window.syscall_top_counts.items(),
        key=lambda kv: kv[1],
        reverse=True,
    )[:10]

    summary: dict[str, Any] = {
        "record_type": "window_summary",
        "window_start_unix_s": round(window.started_ts, 6),
        "window_end_unix_s": round(now_ts, 6),
        "window_sec": window_sec,
        "feature_namespace": "phase1",
        "phase1_metric_contracts": PHASE1_METRIC_CONTRACTS,
        "metrics": {
            "process_churn_rate_per_sec": round(churn_total / window_sec, 3),
            "context_switch_rate_per_sec": round(
                window.event_counts.get("sched_switch", 0) / window_sec, 3
            ),
            "syscall_error_rate": round(err_ratio, 6),
            "syscall_error_rate_per_sec": round(
                window.syscall_error_count / window_sec, 3
            ),
            "rq_latency_p50_us": _quantile(window.rq_latency_samples_us, 0.50),
            "rq_latency_p95_us": _quantile(window.rq_latency_samples_us, 0.95),
            "rq_latency_p99_us": _quantile(window.rq_latency_samples_us, 0.99),
            "rq_latency_count": len(window.rq_latency_samples_us),
        },
        "event_counts": window.event_counts,
        "top_syscalls": [
            {"syscall_id": sid, "count": count} for sid, count in top_syscalls
        ],
        "host_features": host,
        "ml_labels": {
            "bottleneck_class": None,
            "action_outcome": None,
            "effect_size": None,
            "is_training_ready": False,
        },
    }
    return summary


def main() -> int:
    try:
        from bcc import BPF
    except ImportError:
        print(
            "error: Python module 'bcc' not found. Install BCC bindings, e.g.\n"
            "  sudo apt install python3-bpfcc bpfcc-tools\n"
            "Then recreate the venv with system site packages, e.g.\n"
            "  uv venv --system-site-packages --allow-existing && uv sync\n"
            "(scripts/setup_dev_env.sh does this automatically.)",
            file=sys.stderr,
        )
        return 1

    parser = argparse.ArgumentParser(
        description="Run Reflex Phase-1 telemetry collector (raw events + summaries)."
    )
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=_repo_root() / "data" / "mvp_events.jsonl",
        help="Raw events JSONL path (default: data/mvp_events.jsonl)",
    )
    parser.add_argument(
        "--summary-output",
        type=Path,
        default=_repo_root() / "data" / "mvp_summary.jsonl",
        help="Summary JSONL path (default: data/mvp_summary.jsonl)",
    )
    parser.add_argument(
        "--timeout-ms",
        type=int,
        default=200,
        help="ring_buffer_poll timeout in milliseconds",
    )
    parser.add_argument(
        "--window-sec",
        type=float,
        default=1.0,
        help="summary window size in seconds (default: 1.0)",
    )
    parser.add_argument(
        "--proc-sample-sec",
        type=float,
        default=1.0,
        help="sampling interval for /proc-/sys host features (default: 1.0)",
    )
    args = parser.parse_args()

    bpf_c = _repo_root() / "ebpf" / "mvp_ringbuf.bpf.c"
    if not bpf_c.is_file():
        print(f"error: missing eBPF source: {bpf_c}", file=sys.stderr)
        return 1

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.summary_output.parent.mkdir(parents=True, exist_ok=True)
    b = BPF(src_file=str(bpf_c))

    running = True

    def _stop_handler(_sig: int, _frame: Any) -> None:
        nonlocal running
        running = False

    signal.signal(signal.SIGINT, _stop_handler)
    signal.signal(signal.SIGTERM, _stop_handler)

    now = time.time()
    window = WindowState(started_ts=now)
    proc_state = ProcSampleState()
    host_features = proc_state.sample(now)
    next_proc_sample = now + max(args.proc_sample_sec, 0.2)

    with (
        args.output.open("a", encoding="utf-8", buffering=1) as raw_out,
        args.summary_output.open("a", encoding="utf-8", buffering=1) as summary_out,
    ):

        def on_event(_ctx: Any, data: Any, _size: int) -> None:
            ev = b["events"].event(data)
            event = _to_event_dict(ev)
            raw_out.write(json.dumps(event, ensure_ascii=False) + "\n")
            window.add_event(event)

        b["events"].open_ring_buffer(on_event)
        print(
            f"Writing raw events to {args.output} and summaries to "
            f"{args.summary_output}. Ctrl+C to stop.",
            flush=True,
        )

        while running:
            b.ring_buffer_poll(timeout=args.timeout_ms)
            now = time.time()

            if now >= next_proc_sample:
                host_features = proc_state.sample(now)
                next_proc_sample = now + max(args.proc_sample_sec, 0.2)

            if now - window.started_ts >= args.window_sec:
                summary = _build_summary(window, host_features, now, args.window_sec)
                summary_out.write(json.dumps(summary, ensure_ascii=False) + "\n")
                window = WindowState(started_ts=now)

        # Flush final partial window for better test ergonomics.
        now = time.time()
        if window.event_counts or window.sys_exit_count or window.rq_latency_samples_us:
            summary = _build_summary(window, host_features, now, args.window_sec)
            summary_out.write(json.dumps(summary, ensure_ascii=False) + "\n")
        print("Stopped.", flush=True)
        b.cleanup()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
