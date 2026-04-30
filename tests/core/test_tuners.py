from __future__ import annotations

from pathlib import Path

from reflex.core.tuners import TunerAction
from reflex.core.tuners.catalog import ALL_TUNERS
from reflex.core.tuners.schema import TunerCatalogEntry
from reflex.core.tuners.sysctl import GenericSysctlTuner
from reflex.core.tuners.sysctl_util import read_sysctl, write_sysctl


def test_v2_sysctl_tuner_has_no_rollback_method() -> None:
    entry = TunerCatalogEntry(
        id="sysctl_vm_swappiness",
        category="vm",
        description="test",
        kind="int",
        sysctl="vm.swappiness",
    )
    tuner = GenericSysctlTuner(entry)
    assert not hasattr(tuner, "rollback")


def test_v2_catalog_only_has_sysctl_tuners() -> None:
    for t in ALL_TUNERS:
        assert t._entry.scope == "runtime_sysctl"
        assert t._entry.sysctl


def test_v2_sysctl_read_write_int(tmp_path: Path) -> None:
    fake = tmp_path / "swappiness"
    fake.write_text("60\n", encoding="utf-8")
    assert read_sysctl(fake, "int") == 60
    write_sysctl(fake, 55, "int")
    assert read_sysctl(fake, "int") == 55


def test_v2_tuner_apply_captures_previous_value(tmp_path: Path) -> None:
    sysctl_dir = tmp_path / "vm"
    sysctl_dir.mkdir()
    fake = sysctl_dir / "swappiness"
    fake.write_text("60\n", encoding="utf-8")
    entry = TunerCatalogEntry(
        id="sysctl_vm_swappiness",
        category="vm",
        description="test",
        kind="int",
        sysctl="vm.swappiness",
    )
    tuner = GenericSysctlTuner(entry, sysctl_root=tmp_path)

    applied = tuner.apply(
        TunerAction(
            tuner_id="sysctl_vm_swappiness",
            action_id="set",
            target="vm.swappiness",
            value=30,
            reason="test",
        )
    )

    assert applied.previous_value == 60
    assert read_sysctl(fake, "int") == 30


def test_v2_tuner_creates_actions_and_rejects_invalid_values(tmp_path: Path) -> None:
    sysctl_dir = tmp_path / "vm"
    sysctl_dir.mkdir()
    fake = sysctl_dir / "swappiness"
    fake.write_text("60\n", encoding="utf-8")
    entry = TunerCatalogEntry(
        id="sysctl_vm_swappiness",
        category="vm",
        description="test",
        kind="int",
        sysctl="vm.swappiness",
        min_value=0,
        max_value=100,
        step=5,
    )
    tuner = GenericSysctlTuner(entry, sysctl_root=tmp_path)

    valid_step = tuner.create_step_action("decrease", reason="test")
    invalid_step = tuner.create_step_action("increase", steps=9, reason="test")
    valid_set = tuner.create_set_action(55, action_id="set", reason="test")
    invalid_set = tuner.create_set_action(101, action_id="set", reason="test")

    assert valid_step is not None
    assert valid_step.value == 55
    assert invalid_step is None
    assert valid_set is not None
    assert valid_set.value == 55
    assert invalid_set is None
