from __future__ import annotations

import asyncio
import itertools
import time
from dataclasses import dataclass, field
from typing import Any

from daemon_core.config import Daemon
from daemon_core.types import (
    AggregatorSample,
    ControllerTrigger,
    DaemonEvent,
    ExecutionResult,
    Executor,
)


@dataclass
class ScheduledExecutor:
    id: int
    created_ts: float
    executor: Executor
    executor_name: str
    metadata: dict[str, Any] = field(default_factory=dict)
    trigger_id: int | None = None


class _ControllerRunContext:
    def __init__(self, runtime: Runtime, trigger: ControllerTrigger) -> None:
        self._runtime = runtime
        self.trigger = trigger

    async def enqueue_executor(
        self,
        executor: Executor,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        await self._runtime.enqueue_executor(
            executor,
            metadata=metadata or {},
            trigger_id=self.trigger.id,
        )

    async def log_decision(
        self,
        kind: str,
        message: str,
        metadata: dict[str, Any] | None = None,
    ) -> DaemonEvent:
        meta = {"decision_kind": kind, "trigger_id": self.trigger.id}
        meta.update(metadata or {})
        return await self._runtime.log_event("controller_decision", message, meta)

    def executor_queue_size(self) -> int:
        return self._runtime.executor_queue_size()


class Runtime:
    def __init__(self, daemon: Daemon) -> None:
        self.daemon = daemon
        self.sample_queue: asyncio.Queue[AggregatorSample] = asyncio.Queue(
            maxsize=daemon.queue_sizes.samples
        )
        self.controller_queue: asyncio.Queue[ControllerTrigger] = asyncio.Queue(
            maxsize=daemon.queue_sizes.controller_runs
        )
        self.executor_queue: asyncio.Queue[ScheduledExecutor] = asyncio.Queue(
            maxsize=daemon.queue_sizes.executors
        )
        self.events: list[DaemonEvent] = []
        self.execution_results: list[ExecutionResult] = []
        self._event_ids = itertools.count(1)
        self._sample_ids = itertools.count(1)
        self._trigger_ids = itertools.count(1)
        self._executor_ids = itertools.count(1)
        self._stopping = asyncio.Event()
        self._tasks: list[asyncio.Task[Any]] = []

    def _now(self) -> float:
        return time.monotonic()

    async def log_event(
        self,
        kind: str,
        message: str,
        metadata: dict[str, Any] | None = None,
    ) -> DaemonEvent:
        event = DaemonEvent(
            id=next(self._event_ids),
            ts=self._now(),
            kind=kind,
            message=message,
            metadata=metadata or {},
        )
        self.events.append(event)
        return event

    async def _handle_error(self, source: str, exc: BaseException) -> None:
        await self.log_event(
            "error",
            f"{source} failed: {exc}",
            {"source": source, "error_type": type(exc).__name__},
        )
        if self.daemon.on_error is not None:
            try:
                await self.daemon.on_error(self, exc)
            except BaseException as hook_exc:
                await self.log_event(
                    "error",
                    f"on_error hook failed: {hook_exc}",
                    {"source": "on_error", "error_type": type(hook_exc).__name__},
                )
        if self.daemon.terminate_on_user_error:
            await self.stop()

    async def accept_sample(self, sample: Any) -> AggregatorSample:
        wrapped = AggregatorSample(
            id=next(self._sample_ids),
            sent_ts=self._now(),
            sample=sample,
        )
        await self.sample_queue.put(wrapped)
        await self.log_event(
            "sample_received",
            "aggregator sample received",
            {"sample_id": wrapped.id},
        )
        return wrapped

    async def trigger_controller(
        self,
        reason: str,
        metadata: dict[str, Any] | None = None,
    ) -> ControllerTrigger:
        trigger = ControllerTrigger(
            id=next(self._trigger_ids),
            created_ts=self._now(),
            reason=reason,
            metadata=metadata or {},
        )
        await self.controller_queue.put(trigger)
        await self.log_event(
            "trigger_created",
            reason,
            {"trigger_id": trigger.id, **trigger.metadata},
        )
        return trigger

    async def enqueue_executor(
        self,
        executor: Executor,
        metadata: dict[str, Any] | None = None,
        trigger_id: int | None = None,
    ) -> ScheduledExecutor:
        name = type(executor).__name__
        scheduled = ScheduledExecutor(
            id=next(self._executor_ids),
            created_ts=self._now(),
            executor=executor,
            executor_name=name,
            metadata=metadata or {},
            trigger_id=trigger_id,
        )
        await self.executor_queue.put(scheduled)
        await self.log_event(
            "executor_scheduled",
            f"executor scheduled: {name}",
            {
                "executor_id": scheduled.id,
                "executor_name": name,
                "trigger_id": trigger_id,
                **scheduled.metadata,
            },
        )
        return scheduled

    def executor_queue_size(self) -> int:
        return self.executor_queue.qsize()

    async def _sample_worker(self) -> None:
        while not self._stopping.is_set():
            sample = await self.sample_queue.get()
            try:
                await self.daemon.controller.accept_data(sample)
            except asyncio.CancelledError:
                raise
            except BaseException as exc:
                await self._handle_error("controller.accept_data", exc)
            finally:
                self.sample_queue.task_done()

    async def _controller_worker(self) -> None:
        while not self._stopping.is_set():
            trigger = await self.controller_queue.get()
            try:
                await self.log_event(
                    "controller_run_start",
                    f"controller run started: {trigger.reason}",
                    {"trigger_id": trigger.id, **trigger.metadata},
                )
                await self.daemon.controller.run(_ControllerRunContext(self, trigger))
            except asyncio.CancelledError:
                raise
            except BaseException as exc:
                await self._handle_error("controller.run", exc)
            finally:
                self.controller_queue.task_done()

    async def _executor_worker(self) -> None:
        while not self._stopping.is_set():
            scheduled = await self.executor_queue.get()
            try:
                result = await scheduled.executor.execute(self.daemon.dry_run)
            except asyncio.CancelledError:
                raise
            except BaseException as exc:
                result = ExecutionResult(
                    ok=False,
                    dry_run=self.daemon.dry_run,
                    error=f"{type(exc).__name__}: {exc}",
                )
            self.execution_results.append(result)
            await self.log_event(
                "executor_completed",
                f"executor completed: {scheduled.executor_name}",
                {
                    "executor_id": scheduled.id,
                    "executor_name": scheduled.executor_name,
                    "trigger_id": scheduled.trigger_id,
                    "ok": result.ok,
                    "dry_run": result.dry_run,
                    "error": result.error,
                },
            )
            self.executor_queue.task_done()

    async def _run_aggregator(self) -> None:
        try:
            await self.daemon.aggregator.run(self)
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            await self._handle_error("aggregator", exc)

    async def _run_trigger(self, index: int) -> None:
        trigger = self.daemon.triggers[index]
        try:
            await trigger(self)
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            await self._handle_error(f"trigger[{index}]", exc)

    async def start_background(self) -> None:
        if self.daemon.on_start is not None:
            await self.daemon.on_start(self)
        try:
            await self.daemon.aggregator.setup(self)
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            await self._handle_error("aggregator.setup", exc)
            if self.daemon.terminate_on_user_error:
                return
        self._tasks = [
            asyncio.create_task(self._sample_worker()),
            asyncio.create_task(self._controller_worker()),
            asyncio.create_task(self._executor_worker()),
            asyncio.create_task(self._run_aggregator()),
        ]
        for i in range(len(self.daemon.triggers)):
            self._tasks.append(asyncio.create_task(self._run_trigger(i)))
        if self.daemon.on_ready is not None:
            await self.daemon.on_ready(self)

    async def run(self) -> None:
        await self.start_background()
        await self._stopping.wait()
        await self.shutdown()

    async def stop(self) -> None:
        self._stopping.set()

    async def shutdown(self) -> None:
        if self.daemon.on_stop is not None:
            try:
                await self.daemon.on_stop(self)
            except BaseException as exc:
                await self._handle_error("on_stop", exc)
        try:
            await self.daemon.aggregator.stop()
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            await self._handle_error("aggregator.stop", exc)
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
