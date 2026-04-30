from __future__ import annotations

import abc
import time
from dataclasses import dataclass, field
from typing import Any

@dataclass
class TunerAction:
    tuner_id: str
    action_id: str
    target: str
    value: Any
    reason: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class AppliedAction:
    action: TunerAction
    previous_value: Any = None
    applied_unix_s: float = field(default_factory=time.time)
    metadata: dict[str, Any] = field(default_factory=dict)


class BaseTuner(abc.ABC):
    tuner_id: str

    @abc.abstractmethod
    def supports(self) -> bool:
        raise NotImplementedError

    def create_step_action(
        self,
        direction: str,
        *,
        steps: int = 1,
        reason: str,
        metadata: dict[str, Any] | None = None,
    ) -> TunerAction | None:
        return None

    def create_set_action(
        self,
        value: Any,
        *,
        action_id: str,
        reason: str,
        metadata: dict[str, Any] | None = None,
    ) -> TunerAction | None:
        return None

    @abc.abstractmethod
    def apply(self, action: TunerAction, dry_run: bool = False) -> AppliedAction:
        raise NotImplementedError
