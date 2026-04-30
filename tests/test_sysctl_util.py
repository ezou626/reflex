from __future__ import annotations

import tempfile
from pathlib import Path

from reflex.core.tuners.sysctl_util import read_sysctl, sysctl_name_to_path, write_sysctl


def test_sysctl_read_write_int() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        fake = Path(tmp) / "swappiness"
        fake.write_text("60\n", encoding="utf-8")
        assert read_sysctl(fake, "int") == 60
        write_sysctl(fake, 55, "int")
        assert read_sysctl(fake, "int") == 55


def test_sysctl_name_to_path() -> None:
    p = sysctl_name_to_path("net.ipv4.tcp_mem")
    assert p == Path("/proc/sys/net/ipv4/tcp_mem")
