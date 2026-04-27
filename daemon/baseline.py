from __future__ import annotations

import shlex
from pathlib import Path
from typing import Any

from config.loaders import load_tuner_catalog
from tuners.sysctl_util import read_sysctl, sysctl_name_to_path


def parse_proc_cmdline() -> dict[str, str | None]:
    """Best-effort map of kernel cmdline keys to values (None if flag-only)."""
    try:
        text = Path("/proc/cmdline").read_text(encoding="utf-8").strip()
    except OSError:
        return {}
    out: dict[str, str | None] = {}
    try:
        parts = shlex.split(text)
    except ValueError:
        return out
    for p in parts:
        if "=" in p:
            k, _, v = p.partition("=")
            out[k] = v
        else:
            out[p] = None
    return out


def sysctl_baseline_at_start(catalog_path: Path) -> dict[str, Any]:
    """Current /proc/sys values for enabled runtime_sysctl catalog entries."""
    doc = load_tuner_catalog(catalog_path)
    out: dict[str, Any] = {}
    for entry in doc.tuners:
        if entry.scope != "runtime_sysctl" or not entry.enabled:
            continue
        try:
            path = sysctl_name_to_path(entry.sysctl)
            if path.is_file():
                out[entry.sysctl] = read_sysctl(path, entry.kind)
        except (OSError, ValueError):
            continue
    return out
