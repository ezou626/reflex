from __future__ import annotations

import asyncio

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


def test_runtime_event_retention_cap() -> None:
    async def run() -> Runtime:
        daemon = Daemon(
            aggregator=NoopAggregator(),
            controller=NoopController(),
            event_retention=2,
        )
        runtime = Runtime(daemon)
        for idx in range(5):
            await runtime.log_event("test", f"event {idx}")
        return runtime

    runtime = asyncio.run(run())

    assert [event.message for event in runtime.events] == ["event 3", "event 4"]


def test_runtime_retention_can_be_unlimited() -> None:
    async def run() -> Runtime:
        daemon = Daemon(
            aggregator=NoopAggregator(),
            controller=NoopController(),
            event_retention=None,
        )
        runtime = Runtime(daemon)
        for idx in range(3):
            await runtime.log_event("test", f"event {idx}")
        return runtime

    runtime = asyncio.run(run())

    assert len(runtime.events) == 3
