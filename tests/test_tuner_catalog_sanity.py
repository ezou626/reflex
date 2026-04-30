from __future__ import annotations

import pytest

from reflex.core.tuners.catalog import ALL_TUNERS


def test_tuner_catalog_loads() -> None:
    ids = [t.tuner_id for t in ALL_TUNERS]
    assert len(ids) == len(set(ids)), "duplicate tuner IDs in catalog"
    for t in ALL_TUNERS:
        entry = t._entry
        assert entry.id and entry.category and entry.description
        assert entry.kind in ("int", "ints", "str")
        sysctl = entry.sysctl
        assert sysctl and ".." not in sysctl and not sysctl.startswith("/")


@pytest.mark.skip(reason="parse_proc_cmdline and boot_cmdline tuner support removed in src/ restructure")
def test_tuner_constructors() -> None:
    pass


@pytest.mark.skip(reason="TuningPolicy / select_actions removed in src/ restructure; see reflex.core")
def test_select_actions_top_n() -> None:
    pass
