from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import yaml

from config.schema import (
    DecisionSelectionMode,
    RollbackBatchGranularity,
    SysctlKind,
    TunerCatalogDoc,
    TunerCatalogEntry,
    TunerScope,
    TuningPolicy,
)


def _parse_csv(value: str) -> list[str]:
    return [part.strip() for part in value.split(",") if part.strip()]


def load_tuning_policy(path: Path) -> TuningPolicy:
    policy = TuningPolicy()
    if not path.is_file():
        return policy
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if raw is None:
        return policy
    if not isinstance(raw, dict):
        raise ValueError(f"{path}: root must be a mapping")
    if "compare_metrics" in raw and raw["compare_metrics"] is not None:
        cm = raw["compare_metrics"]
        if isinstance(cm, str):
            policy.compare_metrics = _parse_csv(cm)
        elif isinstance(cm, list):
            policy.compare_metrics = [str(x).strip() for x in cm if str(x).strip()]
    for key, typ, conv in (
        ("min_windows_before_action", int, int),
        ("cooldown_windows", int, int),
        ("evaluate_after_windows", int, int),
        ("regression_threshold", float, float),
        ("improvement_threshold", float, float),
        ("max_actions_per_tick", int, int),
        ("min_priority_gap", int, int),
        ("priority_floor", int, int),
        ("max_tracked_applies", int, int),
    ):
        if key in raw and raw[key] is not None:
            setattr(policy, key, conv(raw[key]))
    if "dedupe_by_target" in raw and raw["dedupe_by_target"] is not None:
        policy.dedupe_by_target = bool(raw["dedupe_by_target"])
    if "decision_selection_mode" in raw and raw["decision_selection_mode"] is not None:
        policy.decision_selection_mode = cast(
            DecisionSelectionMode, str(raw["decision_selection_mode"])
        )
    if "rollback_batch_granularity" in raw and raw["rollback_batch_granularity"] is not None:
        policy.rollback_batch_granularity = cast(
            RollbackBatchGranularity, str(raw["rollback_batch_granularity"])
        )
    return policy


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
        tid = _require_str(item, "id", path)
        category = _require_str(item, "category", path)
        description = _require_str(item, "description", path)
        kind = cast(SysctlKind, _require_str(item, "kind", path))
        if kind not in ("int", "ints", "str"):
            raise ValueError(f"{path}: tuners[{i}].kind must be int|ints|str")
        scope = cast(TunerScope, str(item.get("scope", "runtime_sysctl")).strip())
        if scope not in ("runtime_sysctl", "boot_cmdline"):
            raise ValueError(f"{path}: tuners[{i}].scope must be runtime_sysctl|boot_cmdline")
        sysctl = str(item.get("sysctl", "")).strip()
        cmdline_key = item.get("cmdline_key")
        cmdline_key_s = str(cmdline_key).strip() if cmdline_key is not None else None
        if cmdline_key_s == "":
            cmdline_key_s = None
        default_cv = item.get("default_cmdline_value")
        default_cv_s = str(default_cv) if default_cv is not None else None
        enabled = bool(item.get("enabled", True))
        tags_raw = item.get("tags", [])
        tags = [str(t) for t in tags_raw] if isinstance(tags_raw, list) else []
        if scope == "runtime_sysctl" and not sysctl:
            raise ValueError(f"{path}: tuners[{i}] sysctl required for runtime_sysctl")
        if scope == "boot_cmdline" and not cmdline_key_s:
            raise ValueError(f"{path}: tuners[{i}] cmdline_key required for boot_cmdline")
        min_value = item.get("min_value")
        max_value = item.get("max_value")
        step_raw  = item.get("step", 1)
        entries.append(
            TunerCatalogEntry(
                id=tid,
                category=category,
                description=description,
                kind=kind,
                scope=scope,
                sysctl=sysctl,
                cmdline_key=cmdline_key_s,
                default_cmdline_value=default_cv_s,
                enabled=enabled,
                tags=tags,
                min_value=float(min_value) if min_value is not None else None,
                max_value=float(max_value) if max_value is not None else None,
                step=float(step_raw) if step_raw is not None else 1,
            )
        )
    return TunerCatalogDoc(version=version, tuners=entries)
