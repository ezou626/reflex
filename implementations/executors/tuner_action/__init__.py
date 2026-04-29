from __future__ import annotations

from collections.abc import Callable
from typing import Any

from daemon_core.tuners import AppliedAction, TunerAction, TunerRegistry
from daemon_core.types import ExecutionResult


class TunerActionExecutor:
    def __init__(
        self,
        registry: TunerRegistry,
        action: TunerAction,
        *,
        on_applied: Callable[[AppliedAction], None] | None = None,
    ) -> None:
        self.registry = registry
        self.action = action
        self.on_applied = on_applied

    async def execute(self, dry_run: bool) -> ExecutionResult:
        tuner = self.registry.get(self.action.tuner_id)
        if tuner is None:
            return ExecutionResult(
                ok=False,
                dry_run=dry_run,
                error=f"unknown tuner_id: {self.action.tuner_id}",
            )
        applied = tuner.apply(self.action, dry_run=dry_run)
        if self.on_applied is not None:
            self.on_applied(applied)
        record: dict[str, Any] = {
            "tuner_id": self.action.tuner_id,
            "action_id": self.action.action_id,
            "target": self.action.target,
            "value": self.action.value,
            "previous_value": applied.previous_value,
            "metadata": applied.metadata,
        }
        return ExecutionResult(
            ok=True,
            dry_run=dry_run,
            payload=record,
            action_records=[record],
        )
