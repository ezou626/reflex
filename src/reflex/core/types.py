from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


@dataclass(frozen=True)
class AggregatorSample:
    id: int
    sent_ts: float
    sample: Any


@dataclass(frozen=True)
class ControllerTrigger:
    id: int
    created_ts: float
    reason: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class DaemonEvent:
    id: int
    ts: float
    kind: str
    message: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class ExecutionResult:
    ok: bool
    dry_run: bool
    payload: Any = None
    error: str | None = None
    action_records: list[Any] = field(default_factory=list)


@runtime_checkable
class Executor(Protocol):
    async def execute(self, dry_run: bool) -> ExecutionResult:
        ...


@runtime_checkable
class ControllerRunContext(Protocol):
    trigger: ControllerTrigger

    async def enqueue_executor(
        self,
        executor: Executor,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        ...

    async def log_decision(
        self,
        kind: str,
        message: str,
        metadata: dict[str, Any] | None = None,
    ) -> DaemonEvent:
        ...

    def executor_queue_size(self) -> int:
        ...


@runtime_checkable
class Controller(Protocol):
    async def accept_data(self, sample: AggregatorSample) -> None:
        ...

    async def run(self, ctx: ControllerRunContext) -> None:
        ...


@runtime_checkable
class Aggregator(Protocol):
    async def setup(self, runtime: Any) -> None:
        ...

    async def run(self, runtime: Any) -> None:
        ...

    async def stop(self) -> None:
        ...
