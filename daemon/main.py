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
import os
import queue
import signal
import struct
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from action_log import ActionLogger
from applied_stack import AppliedStack
from baseline import parse_proc_cmdline, sysctl_baseline_at_start
from config.loaders import load_tuning_policy
from control import (
    BOProposalController,
    CompositeProposalController,
    ExternalJsonlProposalController,
    HeuristicProposalController,
    WorkloadAwareController,
    WorkloadClassifier,
)
from decision_engine import DecisionEngine
from history import WindowHistory
from rollback import RollbackManager, frames_to_rollback
from tuners.base import AppliedAction
from tuners.registry import TunerRegistry

# struct payload layout from collector.bpf.c (48 bytes, no padding)
# fields: event_type, cpu, pid, tgid, ts_ns, value_i32, value_u32, comm[16]
_PAYLOAD_FMT  = "=IIIIQiI16s"
_PAYLOAD_SIZE = struct.calcsize(_PAYLOAD_FMT)  # 48


class _Ev:
    """Lightweight container for a deserialized payload struct."""
    __slots__ = ("event_type", "cpu", "pid", "tgid", "ts_ns", "value_i32", "value_u32", "comm")

    def __init__(self, fields: tuple) -> None:
        (self.event_type, self.cpu, self.pid, self.tgid,
         self.ts_ns, self.value_i32, self.value_u32, self.comm) = fields


EVENT_EXEC           = 1
EVENT_FORK           = 2
EVENT_EXIT           = 3
EVENT_SCHED_SWITCH   = 4
EVENT_SYSCALL_EXIT   = 5
EVENT_RQ_LATENCY     = 6
EVENT_DIRECT_RECLAIM = 7
EVENT_BLK_LATENCY    = 8

EVENT_NAME = {
    EVENT_EXEC:           "exec",
    EVENT_FORK:           "fork",
    EVENT_EXIT:           "exit",
    EVENT_SCHED_SWITCH:   "sched_switch",
    EVENT_SYSCALL_EXIT:   "sys_exit",
    EVENT_RQ_LATENCY:     "rq_latency",
    EVENT_DIRECT_RECLAIM: "direct_reclaim",
    EVENT_BLK_LATENCY:    "blk_latency",
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


def _default_run_id() -> str:
    return time.strftime("run-%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:8]


def _default_run_dir(run_id: str) -> Path:
    return _repo_root() / "data" / "runs" / run_id


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
    direct_reclaim_samples_us: list[int] = field(default_factory=list)
    blk_latency_samples_us: list[int] = field(default_factory=list)

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
        elif name == "direct_reclaim":
            self.direct_reclaim_samples_us.append(int(event["reclaim_lat_us"]))
        elif name == "blk_latency":
            self.blk_latency_samples_us.append(int(event["blk_lat_us"]))


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
    elif event_type == EVENT_DIRECT_RECLAIM:
        base["reclaim_lat_us"] = int(ev.value_u32)
    elif event_type == EVENT_BLK_LATENCY:
        base["blk_lat_us"] = int(ev.value_u32)
    return base


def _inject_run_baselines(
    summary: dict[str, Any],
    sysctl_baseline: dict[str, Any],
    boot_params: dict[str, str | None],
) -> dict[str, Any]:
    out = dict(summary)
    hf = dict(out.get("host_features", {}))
    hf["sysctl_baseline_at_start"] = dict(sysctl_baseline)
    hf["boot_kernel_params"] = dict(boot_params)
    out["host_features"] = hf
    return out


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
            "direct_reclaim_rate_per_sec": round(
                len(window.direct_reclaim_samples_us) / window_sec, 3
            ),
            "direct_reclaim_lat_p95_us": _quantile(window.direct_reclaim_samples_us, 0.95),
            "blk_latency_p50_us": _quantile(window.blk_latency_samples_us, 0.50),
            "blk_latency_p95_us": _quantile(window.blk_latency_samples_us, 0.95),
            "blk_latency_count": len(window.blk_latency_samples_us),
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
    parser.add_argument(
        "--run-id",
        type=str,
        default="",
        help="Run identifier for decision/action history.",
    )
    parser.add_argument(
        "--run-dir",
        type=Path,
        default=None,
        help="Optional run artifact directory (default: data/runs/<run-id>).",
    )
    parser.add_argument(
        "--decision-log-output",
        type=Path,
        default=None,
        help="Decision/action/rollback JSONL output path.",
    )
    parser.add_argument(
        "--policy-file",
        type=Path,
        default=_repo_root() / "configs" / "tuning_policy.yaml",
        help="Tuning policy config file.",
    )
    parser.add_argument(
        "--tuner-catalog",
        type=Path,
        default=_repo_root() / "configs" / "tuner_catalog.yaml",
        help="Tuner catalog config file.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Emit decisions without changing kernel tunables.",
    )
    parser.add_argument(
        "--external-proposals",
        type=Path,
        default=None,
        help="Optional JSONL file of external TunerAction dicts (one JSON object per line).",
    )
    parser.add_argument(
        "--cgroup-ids",
        nargs="*",
        type=int,
        default=[],
        help="Cgroup IDs to whitelist for syscall filtering (from run.sh). Omit to monitor all.",
    )
    parser.add_argument(
        "--event-trigger-threshold",
        type=int,
        default=8000,
        help="Trigger an early decision tick if window event volume exceeds this.",
    )
    args = parser.parse_args()

    run_id = args.run_id or _default_run_id()
    run_dir = args.run_dir or _default_run_dir(run_id)
    run_dir.mkdir(parents=True, exist_ok=True)
    if args.output == (_repo_root() / "data" / "mvp_events.jsonl"):
        args.output = run_dir / "events.jsonl"
    if args.summary_output == (_repo_root() / "data" / "mvp_summary.jsonl"):
        args.summary_output = run_dir / "summary.jsonl"
    decision_log = args.decision_log_output or (run_dir / "decisions.jsonl")

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.summary_output.parent.mkdir(parents=True, exist_ok=True)
    decision_log.parent.mkdir(parents=True, exist_ok=True)

    policy = load_tuning_policy(args.policy_file)
    boot_params = parse_proc_cmdline()
    sysctl_baseline = sysctl_baseline_at_start(args.tuner_catalog)
    history = WindowHistory(size=max(60, policy.evaluate_after_windows + 5))
    decision_engine = DecisionEngine(policy=policy)
    rollback_manager = RollbackManager(policy=policy)
    tuner_registry = TunerRegistry.from_catalog(
        args.tuner_catalog, boot_params=boot_params
    )
    stack = AppliedStack(
        run_dir / "applied_stack.json", max_tracked=policy.max_tracked_applies
    )
    stack.load()
    heuristic = HeuristicProposalController()
    bo_controller = BOProposalController(
        evaluate_after_windows=policy.evaluate_after_windows,
        fallback=heuristic,
    )
    classifier = WorkloadClassifier.from_model_dir(_repo_root() / "models")
    if classifier.is_loaded():
        print(
            f"Workload classifier loaded: classes={classifier.known_classes()}",
            flush=True,
        )
    workload_controller = WorkloadAwareController(classifier, min_consecutive=3)
    proposal_controllers: list = [workload_controller, bo_controller]
    if args.external_proposals is not None:
        proposal_controllers.append(
            ExternalJsonlProposalController(args.external_proposals)
        )
    proposal_pipeline = CompositeProposalController(proposal_controllers)
    action_logger = ActionLogger(path=decision_log, run_id=run_id)
    should_decide_early = False

    loader_args = ["sudo", "./build/loader", str(os.getpid())]
    loader_args += [str(c) for c in (args.cgroup_ids or [])]
    loader_proc = subprocess.Popen(loader_args, stdout=subprocess.PIPE)
    ev_queue: queue.Queue[_Ev] = queue.Queue()

    def _reader() -> None:
        assert loader_proc.stdout is not None
        while True:
            chunk = loader_proc.stdout.read(_PAYLOAD_SIZE)
            if not chunk or len(chunk) < _PAYLOAD_SIZE:
                break
            ev_queue.put(_Ev(struct.unpack(_PAYLOAD_FMT, chunk)))

    threading.Thread(target=_reader, daemon=True).start()

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
            nonlocal should_decide_early
            ev = data  # already a deserialized _Ev from the reader thread
            event = _to_event_dict(ev)
            raw_out.write(json.dumps(event, ensure_ascii=False) + "\n")
            window.add_event(event)
            if sum(window.event_counts.values()) >= args.event_trigger_threshold:
                should_decide_early = True

        def decision_tick(trigger: str, summary: dict[str, Any]) -> None:
            summary = _inject_run_baselines(summary, sysctl_baseline, boot_params)
            history.add(summary)
            comparison = history.compare_last_two(policy.compare_metrics)
            proposals = proposal_pipeline.propose(
                summary, history.latest(20), registry=tuner_registry
            )
            decision = decision_engine.decide(
                trigger=trigger,
                summary=summary,
                history=history.latest(20),
                proposals=proposals,
            )
            window_id = action_logger.log_decision(
                trigger=trigger,
                decision=decision,
                summary=summary,
                comparison_delta=None if comparison is None else comparison.delta,
            )
            batch_index = 0
            for chosen in decision.chosen_actions:
                tuner = tuner_registry.get(chosen.tuner_id)
                if tuner is None:
                    batch_index += 1
                    continue
                try:
                    applied = tuner.apply(chosen, dry_run=args.dry_run)
                    bo_controller.record_applied(chosen.tuner_id, int(chosen.value), summary)
                    seq = stack.push(applied, window_id, batch_index)
                    depth = stack.depth()
                    action_logger.log_apply(
                        window_id,
                        applied,
                        apply_sequence=seq,
                        stack_depth=depth,
                        stack_index=depth - 1,
                        batch_index=batch_index,
                    )
                except OSError as exc:
                    action_logger.log_rollback(
                        window_id=window_id,
                        applied=AppliedAction(action=chosen, previous_value=None),
                        reason=f"apply_failed:{exc}",
                        effects={},
                        ok=False,
                    )
                batch_index += 1

            rb = rollback_manager.evaluate_for_top_of_stack(stack, history)
            if rb.should_rollback:
                frames = frames_to_rollback(stack, policy)
                for fr in frames:
                    applied = fr.to_applied_action()
                    tuner = tuner_registry.get(fr.tuner_id)
                    ok = False
                    if tuner is not None:
                        try:
                            ok = tuner.rollback(applied, dry_run=args.dry_run)
                        except OSError:
                            ok = False
                    action_logger.log_rollback(
                        window_id=window_id,
                        applied=applied,
                        reason=rb.reason,
                        effects=rb.effects,
                        ok=ok,
                        apply_sequence=fr.apply_sequence,
                        stack_depth=stack.depth(),
                        stack_index=stack.depth(),
                    )
                if frames:
                    decision_engine.note_rollback()
                for fr in frames:
                    bo_controller.record_rollback(fr.tuner_id)

        print(
            f"Writing raw events to {args.output} and summaries to "
            f"{args.summary_output}. Decisions to {decision_log}. "
            f"Run ID: {run_id}. Ctrl+C to stop.",
            flush=True,
        )

        while running:
            deadline = time.monotonic() + args.timeout_ms / 1000.0
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                try:
                    on_event(None, ev_queue.get(timeout=remaining), _PAYLOAD_SIZE)
                except queue.Empty:
                    break
            now = time.time()

            if now >= next_proc_sample:
                host_features = proc_state.sample(now)
                next_proc_sample = now + max(args.proc_sample_sec, 0.2)

            if now - window.started_ts >= args.window_sec:
                summary = _build_summary(window, host_features, now, args.window_sec)
                summary_out.write(json.dumps(summary, ensure_ascii=False) + "\n")
                decision_tick(trigger="timer_window", summary=summary)
                window = WindowState(started_ts=now)
                should_decide_early = False
            elif should_decide_early:
                summary = _build_summary(window, host_features, now, args.window_sec)
                decision_tick(trigger="event_burst", summary=summary)
                should_decide_early = False

        # Flush final partial window for better test ergonomics.
        now = time.time()
        if window.event_counts or window.sys_exit_count or window.rq_latency_samples_us:
            summary = _build_summary(window, host_features, now, args.window_sec)
            summary_out.write(json.dumps(summary, ensure_ascii=False) + "\n")
            decision_tick(trigger="shutdown_flush", summary=summary)
        print("Stopped.", flush=True)
        loader_proc.terminate()
        loader_proc.wait()
    action_logger.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
