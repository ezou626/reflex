from __future__ import annotations

from reflex.core.tuners.base import BaseTuner


class TunerRegistry:
    def __init__(
        self,
        tuners: list[BaseTuner],
        enabled: dict[str, bool] | None = None,
    ) -> None:
        self._tuners = {t.tuner_id: t for t in tuners}
        self._enabled = enabled if enabled is not None else {t.tuner_id: True for t in tuners}

    @classmethod
    def default(cls) -> TunerRegistry:
        from reflex.core.tuners.catalog import ALL_TUNERS
        tuners = list(ALL_TUNERS)
        enabled = {t.tuner_id: t._entry.enabled for t in ALL_TUNERS}
        return cls(tuners=tuners, enabled=enabled)

    def enabled_tuners(self) -> list[BaseTuner]:
        return [t for tid, t in self._tuners.items() if self._enabled.get(tid, False)]

    def get(self, tuner_id: str) -> BaseTuner | None:
        return self._tuners.get(tuner_id)

    def is_enabled(self, tuner_id: str) -> bool:
        return self._enabled.get(tuner_id, False)

    def catalog_entry_ids(self) -> set[str]:
        return set(self._tuners.keys())
