from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def daemon_path() -> None:
    import sys

    d = str(REPO_ROOT / "daemon")
    if d not in sys.path:
        sys.path.insert(0, d)


def test_tuner_catalog_loads(daemon_path: None) -> None:
    from config.loaders import load_tuner_catalog

    doc = load_tuner_catalog(REPO_ROOT / "configs" / "tuner_catalog.yaml")
    ids = [e.id for e in doc.tuners]
    assert len(ids) == len(set(ids))
    for e in doc.tuners:
        assert e.id and e.category and e.description
        assert e.kind in ("int", "ints", "str")
        if e.scope == "runtime_sysctl":
            assert e.sysctl and ".." not in e.sysctl and not e.sysctl.startswith("/")
        if e.scope == "boot_cmdline":
            assert e.cmdline_key


def test_tuner_constructors(daemon_path: None) -> None:
    from baseline import parse_proc_cmdline
    from config.loaders import load_tuner_catalog
    from tuners.sysctl import build_tuner_for_entry

    boot = parse_proc_cmdline()
    doc = load_tuner_catalog(REPO_ROOT / "configs" / "tuner_catalog.yaml")
    for e in doc.tuners:
        t = build_tuner_for_entry(e, boot)
        assert t.tuner_id == e.id
        assert t.supports({"host_features": {}}) in (True, False)


def test_select_actions_top_n(daemon_path: None) -> None:
    from config.schema import TuningPolicy
    from decision_engine import select_actions
    from tuners.base import TunerAction

    p = TuningPolicy(
        decision_selection_mode="top_n_by_priority",
        max_actions_per_tick=2,
    )
    acts = [
        TunerAction("a", "x", "t.a", 1, "", priority=10),
        TunerAction("b", "x", "t.b", 2, "", priority=20),
        TunerAction("c", "x", "t.c", 3, "", priority=15),
    ]
    out = select_actions(acts, p)
    assert [x.tuner_id for x in out] == ["b", "c"]
