from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tuners.base import BaseTuner, TunerAction
from tuners.sysctl import SysctlSwappinessTuner


def _parse_bool(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class TunerConfig:
    tuner_id: str
    enabled: bool = True


def load_tuner_catalog(path: Path) -> dict[str, TunerConfig]:
    if not path.is_file():
        return {}
    configs: dict[str, TunerConfig] = {}
    current_id: str | None = None
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("- id:"):
            current_id = line.split(":", 1)[1].strip()
            configs[current_id] = TunerConfig(tuner_id=current_id, enabled=True)
            continue
        if current_id and line.startswith("enabled:"):
            configs[current_id].enabled = _parse_bool(line.split(":", 1)[1])
    return configs


class TunerRegistry:
    def __init__(self, tuners: list[BaseTuner]) -> None:
        self._tuners = {t.tuner_id: t for t in tuners}
        self._enabled = {t.tuner_id: True for t in tuners}

    @classmethod
    def from_catalog(cls, catalog_path: Path) -> "TunerRegistry":
        tuners: list[BaseTuner] = [SysctlSwappinessTuner()]
        reg = cls(tuners=tuners)
        catalog = load_tuner_catalog(catalog_path)
        for tuner_id, cfg in catalog.items():
            if tuner_id in reg._enabled:
                reg._enabled[tuner_id] = cfg.enabled
        return reg

    def enabled_tuners(self) -> list[BaseTuner]:
        return [t for tid, t in self._tuners.items() if self._enabled.get(tid, False)]

    def collect_proposals(
        self,
        summary: dict[str, Any],
        history: list[dict[str, Any]],
    ) -> list[TunerAction]:
        actions: list[TunerAction] = []
        for tuner in self.enabled_tuners():
            if not tuner.supports(summary):
                continue
            actions.extend(tuner.propose(summary, history))
        return actions

    def get(self, tuner_id: str) -> BaseTuner | None:
        return self._tuners.get(tuner_id)
