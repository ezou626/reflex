from __future__ import annotations

import asyncio
import struct
from typing import Any

from reflex.core.tuners import AppliedAction, TunerAction
from reflex.implementations.aggregators import decode_summary
from reflex.implementations.controllers import (
    ContextualBanditController,
    HeuristicController,
    HillClimbController,
    OpenAITuningController,
    WorkloadClassifier,
    WorkloadClassifierController,
)
from reflex.implementations.controllers.workload_classifier import DEFAULT_LIBRARY_PATH
from reflex.implementations.executors import BatchTunerExecutor, TunerActionExecutor
from reflex.implementations.main import _load_daemon_configs


class FakeTuner:
    def __init__(self) -> None:
        self.applied: list[TunerAction] = []

    def apply(self, action: TunerAction, dry_run: bool = False) -> AppliedAction:
        self.applied.append(action)
        return AppliedAction(action=action, previous_value=0, metadata={"dry_run": dry_run})


class FakeRegistry:
    def __init__(self, tuners: dict[str, FakeTuner]) -> None:
        self.tuners = tuners

    def get(self, tuner_id: str) -> Any:
        return self.tuners.get(tuner_id)


def test_reflex_implementation_imports() -> None:
    assert ContextualBanditController is not None
    assert HeuristicController is not None
    assert HillClimbController is not None
    assert OpenAITuningController is not None
    assert WorkloadClassifier is not None
    assert WorkloadClassifierController is not None
    assert BatchTunerExecutor is not None
    assert TunerActionExecutor is not None


def test_current_payload_decoder_summary_record() -> None:
    chunk = struct.pack("=QIIIIIII", 99, 45, 100, 5, 67, 300, 2, 4)
    summary = decode_summary(chunk, window_sec=2.0, received_ts=1000.0)
    assert summary["record_type"] == "window_summary"
    assert summary["loader_window_end_ns"] == 99
    assert summary["metrics"]["rq_latency_p95_us"] == 45
    assert summary["metrics"]["syscall_error_rate"] == 0.05
    assert summary["event_counts"]["sched_switch"] == 300


def test_reflex_main_discovers_daemon_configs() -> None:
    configs = _load_daemon_configs()
    assert set(configs) >= {"heuristic", "classifier", "openai", "hillclimb", "bandit"}
    assert "bo" not in configs


def test_workload_classifier_library_moved_with_controller() -> None:
    classifier = WorkloadClassifier(DEFAULT_LIBRARY_PATH)
    assert DEFAULT_LIBRARY_PATH.is_file()
    assert classifier.is_loaded()


def test_batch_tuner_executor_executes_all_actions_in_one_result() -> None:
    async def scenario() -> None:
        tuner_a = FakeTuner()
        tuner_b = FakeTuner()
        executor = BatchTunerExecutor(
            FakeRegistry({"a": tuner_a, "b": tuner_b}),
            [
                TunerAction("a", "set_a", "vm.a", 1, "test"),
                TunerAction("b", "set_b", "vm.b", 2, "test"),
            ],
        )
        result = await executor.execute(dry_run=True)

        assert result.ok
        assert result.dry_run
        assert len(result.action_records) == 2
        assert [record["tuner_id"] for record in result.action_records] == ["a", "b"]
        assert [action.value for action in tuner_a.applied + tuner_b.applied] == [1, 2]

    asyncio.run(scenario())
