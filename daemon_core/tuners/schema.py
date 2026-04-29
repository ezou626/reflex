from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

SysctlKind = Literal["int", "ints", "str"]
TunerScope = Literal["runtime_sysctl"]


@dataclass
class TunerCatalogEntry:
    id: str
    category: str
    description: str
    kind: SysctlKind
    scope: TunerScope = "runtime_sysctl"
    sysctl: str = ""
    enabled: bool = True
    tags: list[str] = field(default_factory=list)
    min_value: int | float | None = None
    max_value: int | float | None = None
    step: int | float = 1


@dataclass
class TunerCatalogDoc:
    version: int = 1
    tuners: list[TunerCatalogEntry] = field(default_factory=list)
