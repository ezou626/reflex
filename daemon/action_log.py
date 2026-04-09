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
        entry = {
            "record_type": "decision",
            "run_id": self.run_id,
            "window_id": self._window_id,
            "timestamp": round(time.time(), 6),
            "trigger": trigger,
            "reason": decision.reason,
            "candidate_actions": decision.candidate_actions,
            "chosen_action": None
            if decision.chosen_action is None
            else {
                "tuner_id": decision.chosen_action.tuner_id,
                "action_id": decision.chosen_action.action_id,
                "target": decision.chosen_action.target,
                "value": decision.chosen_action.value,
                "reason": decision.chosen_action.reason,
                "priority": decision.chosen_action.priority,
                "metadata": decision.chosen_action.metadata,
            },
            "window_metrics": summary.get("metrics", {}),
            "window_host_features": summary.get("host_features", {}),
            "window_delta": comparison_delta or {},
        }
        self._write(entry)
        return self._window_id

    def log_apply(self, window_id: int, applied: AppliedAction) -> None:
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
            }
        )

    def log_rollback(
        self,
        window_id: int,
        applied: AppliedAction,
        reason: str,
        effects: dict[str, float],
        ok: bool,
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
            }
        )
