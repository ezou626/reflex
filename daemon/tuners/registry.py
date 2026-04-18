from __future__ import annotations

from pathlib import Path
from typing import Any

from config.loaders import load_tuner_catalog

from tuners.base import BaseTuner
from tuners.sysctl import build_tuner_for_entry


class TunerRegistry:
    def __init__(
        self,
        tuners: list[BaseTuner],
        enabled: dict[str, bool] | None = None,
    ) -> None:
        self._tuners = {t.tuner_id: t for t in tuners}
        self._enabled = enabled if enabled is not None else {t.tuner_id: True for t in tuners}

    @classmethod
    def from_catalog(
        cls,
        catalog_path: Path,
        boot_params: dict[str, str | None] | None = None,
    ) -> "TunerRegistry":
        boot_params = boot_params or {}
        doc = load_tuner_catalog(catalog_path)
        tuners: list[BaseTuner] = []
        enabled: dict[str, bool] = {}
        for entry in doc.tuners:
            tuners.append(build_tuner_for_entry(entry, boot_params))
            enabled[entry.id] = entry.enabled
        return cls(tuners=tuners, enabled=enabled)

    def enabled_tuners(self) -> list[BaseTuner]:
        return [t for tid, t in self._tuners.items() if self._enabled.get(tid, False)]

    def get(self, tuner_id: str) -> BaseTuner | None:
        return self._tuners.get(tuner_id)

    def is_enabled(self, tuner_id: str) -> bool:
        return self._enabled.get(tuner_id, False)

    def catalog_entry_ids(self) -> set[str]:
        return set(self._tuners.keys())
