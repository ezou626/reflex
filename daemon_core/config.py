from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from daemon_core.types import Aggregator, Controller


RuntimeHook = Callable[[Any], Awaitable[None]]
ErrorHook = Callable[[Any, BaseException], Awaitable[None]]
TriggerFactory = Callable[[Any], Awaitable[None]]


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
    terminate_on_user_error: bool = False
    on_start: RuntimeHook | None = None
    on_ready: RuntimeHook | None = None
    on_stop: RuntimeHook | None = None
    on_error: ErrorHook | None = None
