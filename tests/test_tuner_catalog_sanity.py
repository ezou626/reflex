from __future__ import annotations

from pathlib import Path

import pytest

from reflex.core.tuners.loaders import load_tuner_catalog

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_tuner_catalog_loads() -> None:
    doc = load_tuner_catalog(REPO_ROOT / "configs" / "tuner_catalog.yaml")
    ids = [e.id for e in doc.tuners]
    assert len(ids) == len(set(ids))
    for e in doc.tuners:
        assert e.id and e.category and e.description
        assert e.kind in ("int", "ints", "str")
        if e.scope == "runtime_sysctl":
            assert e.sysctl and ".." not in e.sysctl and not e.sysctl.startswith("/")


@pytest.mark.skip(reason="parse_proc_cmdline and boot_cmdline tuner support removed in src/ restructure")
def test_tuner_constructors() -> None:
    pass


@pytest.mark.skip(reason="TuningPolicy / select_actions removed in src/ restructure; see reflex.core")
def test_select_actions_top_n() -> None:
    pass
