from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from reflex.core.types import Aggregator, Controller


RuntimeHook = Callable[[Any], Awaitable[None]]
ErrorHook = Callable[[Any, BaseException], Awaitable[None]]
TriggerFactory = Callable[[Any], Awaitable[None]]


def interval_trigger(interval_sec: float, reason: str = "interval_timer") -> TriggerFactory:
    """Return a trigger that fires runtime.trigger_controller every interval_sec seconds."""
    async def _trigger(runtime: Any) -> None:
        while True:
            await asyncio.sleep(interval_sec)
            await runtime.trigger_controller(reason)
    return _trigger


@dataclass(frozen=True)
class QueueSizes:
    samples: int = 1024
    controller_runs: int = 128
    executors: int = 128


@dataclass
class Daemon:
    aggregator: Aggregator
    controller: Controller
    triggers: list[TriggerFactory] = field(default_factory=list)
    queue_sizes: QueueSizes = field(default_factory=QueueSizes)
    dry_run: bool = False
    event_retention: int | None = 4096
    execution_result_retention: int | None = 1024
    terminate_on_user_error: bool = False
    on_start: RuntimeHook | None = None
    on_ready: RuntimeHook | None = None
    on_stop: RuntimeHook | None = None
    on_error: ErrorHook | None = None
