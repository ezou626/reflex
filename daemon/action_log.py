from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from decision_engine import Decision
from tuners.base import AppliedAction


class ActionLogger:
    def __init__(self, path: Path, run_id: str) -> None:
        self.path = path
        self.run_id = run_id
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = self.path.open("a", encoding="utf-8", buffering=1)
        self._window_id = 0

    def close(self) -> None:
        self._handle.close()

    def _write(self, payload: dict[str, Any]) -> None:
        self._handle.write(json.dumps(payload, ensure_ascii=False) + "\n")

    def log_decision(
        self,
        trigger: str,
        decision: Decision,
        summary: dict[str, Any],
        comparison_delta: dict[str, float] | None,
    ) -> int:
        self._window_id += 1
        chosen_list = [
            {
                "tuner_id": a.tuner_id,
                "action_id": a.action_id,
                "target": a.target,
                "value": a.value,
                "reason": a.reason,
                "priority": a.priority,
                "metadata": a.metadata,
            }
            for a in decision.chosen_actions
        ]
        entry = {
            "record_type": "decision",
            "run_id": self.run_id,
            "window_id": self._window_id,
            "timestamp": round(time.time(), 6),
            "trigger": trigger,
            "reason": decision.reason,
            "candidate_actions": decision.candidate_actions,
            "chosen_actions": chosen_list,
            "window_metrics": summary.get("metrics", {}),
            "window_host_features": summary.get("host_features", {}),
            "window_delta": comparison_delta or {},
        }
        self._write(entry)
        return self._window_id

    def log_apply(
        self,
        window_id: int,
        applied: AppliedAction,
        *,
        apply_sequence: int,
        stack_depth: int,
        stack_index: int,
        batch_index: int,
    ) -> None:
        self._write(
            {
                "record_type": "action_apply",
                "run_id": self.run_id,
                "window_id": window_id,
                "timestamp": round(time.time(), 6),
                "tuner_id": applied.action.tuner_id,
                "action_id": applied.action.action_id,
                "target": applied.action.target,
                "value": applied.action.value,
                "previous_value": applied.previous_value,
                "metadata": applied.metadata,
                "apply_sequence": apply_sequence,
                "stack_depth": stack_depth,
                "stack_index": stack_index,
                "batch_index": batch_index,
            }
        )

    def log_rollback(
        self,
        window_id: int,
        applied: AppliedAction,
        reason: str,
        effects: dict[str, float],
        ok: bool,
        *,
        apply_sequence: int = -1,
        stack_depth: int = -1,
        stack_index: int = -1,
    ) -> None:
        self._write(
            {
                "record_type": "rollback",
                "run_id": self.run_id,
                "window_id": window_id,
                "timestamp": round(time.time(), 6),
                "tuner_id": applied.action.tuner_id,
                "action_id": applied.action.action_id,
                "target": applied.action.target,
                "value": applied.action.value,
                "restore_value": applied.previous_value,
                "rollback_ok": ok,
                "reason": reason,
                "effects": effects,
                "apply_sequence": apply_sequence,
                "stack_depth": stack_depth,
                "stack_index": stack_index,
            }
        )
