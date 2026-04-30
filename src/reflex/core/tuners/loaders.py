from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import yaml

from reflex.core.tuners.schema import SysctlKind, TunerCatalogDoc, TunerCatalogEntry


def _require_str(d: dict[str, Any], key: str, path: Path) -> str:
    if key not in d or d[key] is None or str(d[key]).strip() == "":
        raise ValueError(f"{path}: tuners[].{key} is required")
    return str(d[key]).strip()


def load_tuner_catalog(path: Path) -> TunerCatalogDoc:
    if not path.is_file():
        return TunerCatalogDoc()
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if raw is None:
        return TunerCatalogDoc()
    if not isinstance(raw, dict):
        raise ValueError(f"{path}: root must be a mapping")
    version = int(raw.get("version", 1))
    tuners_raw = raw.get("tuners")
    if tuners_raw is None:
        return TunerCatalogDoc(version=version, tuners=[])
    if not isinstance(tuners_raw, list):
        raise ValueError(f"{path}: tuners must be a list")

    entries: list[TunerCatalogEntry] = []
    for i, item in enumerate(tuners_raw):
        if not isinstance(item, dict):
            raise ValueError(f"{path}: tuners[{i}] must be a mapping")
        scope = str(item.get("scope", "runtime_sysctl")).strip()
        if scope != "runtime_sysctl":
            continue
        kind = cast(SysctlKind, _require_str(item, "kind", path))
        if kind not in ("int", "ints", "str"):
            raise ValueError(f"{path}: tuners[{i}].kind must be int|ints|str")
        sysctl = str(item.get("sysctl", "")).strip()
        if not sysctl:
            raise ValueError(f"{path}: tuners[{i}] sysctl required for runtime_sysctl")
        tags_raw = item.get("tags", [])
        entries.append(
            TunerCatalogEntry(
                id=_require_str(item, "id", path),
                category=_require_str(item, "category", path),
                description=_require_str(item, "description", path),
                kind=kind,
                scope="runtime_sysctl",
                sysctl=sysctl,
                enabled=bool(item.get("enabled", True)),
                tags=[str(t) for t in tags_raw] if isinstance(tags_raw, list) else [],
                min_value=(
                    float(item["min_value"]) if item.get("min_value") is not None else None
                ),
                max_value=(
                    float(item["max_value"]) if item.get("max_value") is not None else None
                ),
                step=float(item.get("step", 1)),
            )
        )
    return TunerCatalogDoc(version=version, tuners=entries)
