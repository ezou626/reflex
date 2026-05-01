from __future__ import annotations

import asyncio
from typing import Any

from reflex.core import AggregatorSample, Daemon, ExecutionResult, QueueSizes, Runtime


class IdleAggregator:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def setup(self, runtime: Runtime) -> None:
        self.calls.append("setup")

    async def run(self, runtime: Runtime) -> None:
        self.calls.append("run")
        await runtime._stopping.wait()

    async def stop(self) -> None:
        self.calls.append("stop")


class RecordingController:
    def __init__(self) -> None:
        self.samples: list[AggregatorSample] = []
        self.triggers: list[int] = []

    async def accept_data(self, sample: AggregatorSample) -> None:
        self.samples.append(sample)

    async def run(self, ctx: Any) -> None:
        self.triggers.append(ctx.trigger.id)
        await ctx.log_decision("test", "controller decided", {"trigger": ctx.trigger.id})


class QueueingController(RecordingController):
    def __init__(self, seen: list[str]) -> None:
        super().__init__()
        self.seen = seen

    async def run(self, ctx: Any) -> None:
        await super().run(ctx)
        await ctx.enqueue_executor(RecordingExecutor("a", self.seen), {"slot": "a"})
        await ctx.enqueue_executor(RecordingExecutor("b", self.seen), {"slot": "b"})


class RecordingExecutor:
    def __init__(self, name: str, seen: list[str]) -> None:
        self.name = name
        self.seen = seen

    async def execute(self, dry_run: bool) -> ExecutionResult:
        self.seen.append(self.name)
        return ExecutionResult(ok=True, dry_run=dry_run, payload={"name": self.name})


class FailingExecutor:
    async def execute(self, dry_run: bool) -> ExecutionResult:
        raise RuntimeError("boom")


class FailingController:
    async def accept_data(self, sample: AggregatorSample) -> None:
        raise ValueError(f"bad sample {sample.id}")

    async def run(self, ctx: Any) -> None:
        raise RuntimeError(f"bad trigger {ctx.trigger.id}")


def run(coro: Any) -> Any:
    return asyncio.run(coro)


def test_accept_sample_blocks_when_sample_queue_full() -> None:
    async def scenario() -> None:
        controller = RecordingController()
        runtime = Runtime(
            Daemon(
                aggregator=IdleAggregator(),
                controller=controller,
                queue_sizes=QueueSizes(samples=1),
            )
        )
        await runtime.accept_sample("first")
        blocked = asyncio.create_task(runtime.accept_sample("second"))
        try:
            await asyncio.wait_for(asyncio.shield(blocked), timeout=0.2)
        except TimeoutError:
            pass
        else:
            raise AssertionError("accept_sample did not block on a full sample queue")
        blocked.cancel()
        await asyncio.gather(blocked, return_exceptions=True)

    run(scenario())


def test_trigger_controller_blocks_when_controller_queue_full() -> None:
    async def scenario() -> None:
        controller = RecordingController()
        runtime = Runtime(
            Daemon(
                aggregator=IdleAggregator(),
                controller=controller,
                queue_sizes=QueueSizes(controller_runs=1),
            )
        )
        await runtime.trigger_controller("first")
        blocked = asyncio.create_task(runtime.trigger_controller("second"))
        try:
            await asyncio.wait_for(asyncio.shield(blocked), timeout=0.2)
        except TimeoutError:
            pass
        else:
            raise AssertionError("trigger_controller did not block on a full queue")
        blocked.cancel()
        await asyncio.gather(blocked, return_exceptions=True)

    run(scenario())


def test_samples_are_delivered_in_arrival_order_without_parallel_accept_data() -> None:
    async def scenario() -> None:
        controller = RecordingController()
        runtime = Runtime(
            Daemon(aggregator=IdleAggregator(), controller=controller)
        )
        await runtime.start_background()
        await runtime.accept_sample({"n": 1})
        await runtime.accept_sample({"n": 2})
        await runtime.sample_queue.join()
        await runtime.shutdown()

        assert [s.id for s in controller.samples] == [1, 2]
        assert [s.sample for s in controller.samples] == [{"n": 1}, {"n": 2}]

    run(scenario())


def test_each_trigger_creates_exactly_one_controller_run() -> None:
    async def scenario() -> None:
        controller = RecordingController()
        runtime = Runtime(
            Daemon(aggregator=IdleAggregator(), controller=controller)
        )
        await runtime.start_background()
        await runtime.trigger_controller("tick")
        await runtime.trigger_controller("tick")
        await runtime.controller_queue.join()
        await runtime.shutdown()

        assert controller.triggers == [1, 2]

    run(scenario())


def test_executor_queue_is_fifo_single_consumer_and_dry_run_is_passed() -> None:
    async def scenario() -> None:
        seen: list[str] = []
        controller = QueueingController(seen)
        runtime = Runtime(
            Daemon(
                aggregator=IdleAggregator(),
                controller=controller,
                dry_run=True,
            )
        )
        await runtime.start_background()
        await runtime.trigger_controller("tick", {"why": "test"})
        await runtime.controller_queue.join()
        await runtime.executor_queue.join()
        await runtime.shutdown()

        assert seen == ["a", "b"]
        assert [r.dry_run for r in runtime.execution_results] == [True, True]

    run(scenario())


def test_executor_exception_becomes_failed_execution_result() -> None:
    async def scenario() -> None:
        controller = RecordingController()
        runtime = Runtime(
            Daemon(aggregator=IdleAggregator(), controller=controller)
        )
        await runtime.start_background()
        await runtime.enqueue_executor(FailingExecutor())
        await runtime.executor_queue.join()
        await runtime.shutdown()

        assert len(runtime.execution_results) == 1
        result = runtime.execution_results[0]
        assert not result.ok
        assert result.error is not None
        assert "RuntimeError: boom" in result.error

    run(scenario())


def test_controller_failures_are_logged_without_stopping_daemon() -> None:
    async def scenario() -> None:
        runtime = Runtime(
            Daemon(aggregator=IdleAggregator(), controller=FailingController())
        )
        await runtime.start_background()
        await runtime.accept_sample("bad")
        await runtime.trigger_controller("bad")
        await runtime.sample_queue.join()
        await runtime.controller_queue.join()
        assert not runtime._stopping.is_set()
        await runtime.shutdown()

    run(scenario())


def test_aggregator_setup_and_stop_are_called() -> None:
    async def scenario() -> None:
        aggregator = IdleAggregator()
        runtime = Runtime(
            Daemon(aggregator=aggregator, controller=RecordingController())
        )
        await runtime.start_background()
        await asyncio.sleep(0)
        await runtime.shutdown()

        assert aggregator.calls == ["setup", "run", "stop"]

    run(scenario())
