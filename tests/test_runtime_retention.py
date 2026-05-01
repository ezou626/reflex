from __future__ import annotations

from reflex.core import Daemon, Runtime


class NoopAggregator:
    async def setup(self, runtime) -> None:
        pass

    async def run(self, runtime) -> None:
        pass

    async def stop(self) -> None:
        pass


class NoopController:
    async def accept_data(self, sample) -> None:
        pass

    async def run(self, ctx) -> None:
        pass


def test_execution_result_retention_cap() -> None:
    runtime = Runtime(
        Daemon(
            aggregator=NoopAggregator(),
            controller=NoopController(),
            execution_result_retention=2,
        )
    )

    for idx in range(5):
        runtime.record_execution_result(ok=True, payload={"idx": idx})

    assert [r.payload for r in runtime.execution_results] == [{"idx": 3}, {"idx": 4}]


def test_execution_result_retention_can_be_unlimited() -> None:
    runtime = Runtime(
        Daemon(
            aggregator=NoopAggregator(),
            controller=NoopController(),
            execution_result_retention=None,
        )
    )

    for idx in range(3):
        runtime.record_execution_result(ok=True, payload={"idx": idx})

    assert [r.payload for r in runtime.execution_results] == [
        {"idx": 0},
        {"idx": 1},
        {"idx": 2},
    ]
