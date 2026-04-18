from __future__ import annotations

from pathlib import Path
from typing import Any

from config.schema import SysctlKind


def sysctl_name_to_path(name: str) -> Path:
    name = name.strip()
    if not name or name.startswith("/") or ".." in name or " " in name:
        raise ValueError(f"invalid sysctl name: {name!r}")
    parts = name.split(".")
    if any(not p or ".." in p for p in parts):
        raise ValueError(f"invalid sysctl name: {name!r}")
    return Path("/proc/sys").joinpath(*parts)


def read_sysctl(path: Path, kind: SysctlKind) -> Any:
    raw = path.read_text(encoding="utf-8").strip()
    if kind == "int":
        return int(raw.split()[0])
    if kind == "ints":
        return [int(x) for x in raw.replace("\t", " ").split()]
    return raw


def format_sysctl_value(value: Any, kind: SysctlKind) -> str:
    if kind == "int":
        return f"{int(value)}\n"
    if kind == "ints":
        if not isinstance(value, (list, tuple)):
            raise TypeError("ints sysctl value must be list/tuple")
        xs = [int(x) for x in value]
        return "\t".join(str(x) for x in xs) + "\n"
    return f"{value}\n"


def write_sysctl(path: Path, value: Any, kind: SysctlKind) -> None:
    path.write_text(format_sysctl_value(value, kind), encoding="utf-8")
