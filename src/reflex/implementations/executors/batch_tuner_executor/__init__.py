from __future__ import annotations

from collections.abc import Callable
from typing import Any

from reflex.core.tuners import AppliedAction, TunerAction, TunerRegistry
from reflex.core.types import ExecutionResult


class BatchTunerExecutor:
    def __init__(
        self,
        registry: TunerRegistry,
        actions: list[TunerAction],
        *,
        on_applied: Callable[[AppliedAction], None] | None = None,
    ) -> None:
        self.registry = registry
        self.actions = actions
        self.on_applied = on_applied

    async def execute(self, dry_run: bool) -> ExecutionResult:
        records: list[dict[str, Any]] = []
        for action in self.actions:
            tuner = self.registry.get(action.tuner_id)
            if tuner is None:
                return ExecutionResult(
                    ok=False,
                    dry_run=dry_run,
                    payload=records,
                    error=f"unknown tuner_id: {action.tuner_id}",
                    action_records=records,
                )
            applied = tuner.apply(action, dry_run=dry_run)
            if self.on_applied is not None:
                self.on_applied(applied)
            records.append({
                "tuner_id": action.tuner_id,
                "action_id": action.action_id,
                "target": action.target,
                "value": action.value,
                "previous_value": applied.previous_value,
                "metadata": applied.metadata,
            })
        return ExecutionResult(
            ok=True,
            dry_run=dry_run,
            payload=records,
            action_records=records,
        )
