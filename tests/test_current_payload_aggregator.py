from __future__ import annotations

import asyncio
import struct

import pytest

import reflex.implementations.aggregators.window_summary as window_summary
from reflex.implementations.aggregators.window_summary import (
    WindowSummaryAggregator,
    decode_summary,
)


def _summary_bytes(
    *,
    window_end_ns: int = 123,
    rq_p95_us: int = 45,
    rq_latency_count: int = 8,
    syscall_count: int = 100,
    failure_count: int = 5,
    blk_p95_us: int = 67,
    blk_latency_count: int = 9,
    ctx_switch_count: int = 300,
    direct_reclaim_count: int = 2,
    direct_reclaim_p95_us: int = 89,
    fork_count: int = 4,
) -> bytes:
    return struct.pack(
        window_summary._SUMMARY_FMT,
        window_end_ns,
        rq_p95_us,
        rq_latency_count,
        syscall_count,
        failure_count,
        blk_p95_us,
        blk_latency_count,
        ctx_switch_count,
        direct_reclaim_count,
        direct_reclaim_p95_us,
        fork_count,
    )


def test_decode_summary_matches_controller_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(window_summary, "_host_features", lambda: {"host_dirty_kb": 10})

    summary = decode_summary(_summary_bytes(), window_sec=2.0, received_ts=1000.0)

    assert summary["record_type"] == "window_summary"
    assert summary["window_start_unix_s"] == 998.0
    assert summary["loader_window_end_ns"] == 123
    assert summary["metrics"]["rq_latency_p95_us"] == 45
    assert summary["metrics"]["rq_latency_count"] == 8
    assert summary["metrics"]["blk_latency_p95_us"] == 67
    assert summary["metrics"]["blk_latency_count"] == 9
    assert summary["metrics"]["direct_reclaim_lat_p95_us"] == 89
    assert summary["metrics"]["syscall_error_rate"] == 0.05
    assert summary["metrics"]["syscall_error_rate_per_sec"] == 2.5
    assert summary["metrics"]["context_switch_rate_per_sec"] == 150.0
    assert summary["metrics"]["direct_reclaim_rate_per_sec"] == 1.0
    assert summary["metrics"]["process_churn_rate_per_sec"] == 2.0
    assert summary["event_counts"] == {
        "fork": 4,
        "sched_switch": 300,
        "sys_exit": 100,
        "direct_reclaim": 2,
    }
    assert summary["top_syscalls"] == []
    assert summary["host_features"] == {"host_dirty_kb": 10}
    assert "rq_latency_p50_us" not in summary["metrics"]
    assert "rq_latency_p99_us" not in summary["metrics"]
    assert "blk_latency_p50_us" not in summary["metrics"]


def test_aggregator_reads_summary_records(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(window_summary, "_host_features", lambda: {})

    class FakeStdout:
        def __init__(self, chunks: list[bytes]) -> None:
            self.chunks = chunks

        async def readexactly(self, n: int) -> bytes:
            assert n == window_summary._SUMMARY_SIZE
            if not self.chunks:
                raise asyncio.IncompleteReadError(partial=b"", expected=n)
            return self.chunks.pop(0)

    class FakeProcess:
        def __init__(self) -> None:
            self.stdout = FakeStdout([_summary_bytes(syscall_count=7, failure_count=1)])

    class FakeRuntime:
        def __init__(self) -> None:
            self.samples: list[dict] = []
            self.triggers: list[tuple[str, dict]] = []
            self.stopped = False

        async def accept_sample(self, sample: dict) -> None:
            self.samples.append(sample)

        async def trigger_controller(self, reason: str, metadata: dict) -> None:
            self.triggers.append((reason, metadata))

        async def log_event(self, *_args, **_kwargs) -> None:
            pass

        async def stop(self) -> None:
            self.stopped = True

    async def run_once() -> FakeRuntime:
        runtime = FakeRuntime()
        aggregator = WindowSummaryAggregator(["loader"], window_sec=1.0)
        aggregator.process = FakeProcess()  # type: ignore[assignment]
        await aggregator.run(runtime)
        return runtime

    runtime = asyncio.run(run_once())

    assert runtime.stopped is True
    assert len(runtime.samples) == 1
    assert runtime.samples[0]["metrics"]["syscall_error_rate"] == pytest.approx(1 / 7)
    assert runtime.triggers == [
        (
            "timer_window",
            {"window_start_unix_s": runtime.samples[0]["window_start_unix_s"]},
        )
    ]
