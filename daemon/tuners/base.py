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
    priority: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class AppliedAction:
    action: TunerAction
    previous_value: Any
    applied_unix_s: float = field(default_factory=time.time)
    metadata: dict[str, Any] = field(default_factory=dict)


class BaseTuner(abc.ABC):
    tuner_id: str

    @abc.abstractmethod
    def supports(self, summary: dict[str, Any]) -> bool:
        raise NotImplementedError

    @abc.abstractmethod
    def apply(self, action: TunerAction, dry_run: bool = False) -> AppliedAction:
        raise NotImplementedError

    @abc.abstractmethod
    def rollback(self, applied: AppliedAction, dry_run: bool = False) -> bool:
        raise NotImplementedError
