from __future__ import annotations

import asyncio
from typing import Any

from reflex.core.tuners import AppliedAction, TunerAction
from reflex.implementations.executors import BatchTunerExecutor
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


def test_reflex_main_discovers_daemon_configs() -> None:
    configs = _load_daemon_configs()
    assert set(configs) >= {"heuristic", "classifier", "openai", "hillclimb", "bandit"}
    assert "bo" not in configs


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
