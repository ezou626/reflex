from __future__ import annotations

from pathlib import Path

from reflex.core.tuners import TunerAction
from reflex.core.tuners.loaders import load_tuner_catalog
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


def test_v2_catalog_loader_ignores_boot_cmdline_tuners(tmp_path: Path) -> None:
    catalog = tmp_path / "catalog.yaml"
    catalog.write_text(
        """
version: 1
tuners:
  - id: sysctl_vm_swappiness
    category: vm
    description: runtime
    kind: int
    scope: runtime_sysctl
    sysctl: vm.swappiness
  - id: boot_numa_balancing
    category: boot
    description: boot
    kind: str
    scope: boot_cmdline
    cmdline_key: numa_balancing
""",
        encoding="utf-8",
    )
    doc = load_tuner_catalog(catalog)
    assert [t.id for t in doc.tuners] == ["sysctl_vm_swappiness"]


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
