from __future__ import annotations

import abc
import json
from pathlib import Path
from typing import Any

from tuners.base import TunerAction
from tuners.registry import TunerRegistry
from tuners.sysctl_util import read_sysctl, sysctl_name_to_path


class ProposalController(abc.ABC):
    """Produces tuning candidates; effectors live on BaseTuner."""

    @abc.abstractmethod
    def propose(
        self,
        summary: dict[str, Any],
        history: list[dict[str, Any]],
        *,
        registry: TunerRegistry,
    ) -> list[TunerAction]:
        raise NotImplementedError


class CompositeProposalController(ProposalController):
    def __init__(self, controllers: list[ProposalController]) -> None:
        self._controllers = controllers

    def propose(
        self,
        summary: dict[str, Any],
        history: list[dict[str, Any]],
        *,
        registry: TunerRegistry,
    ) -> list[TunerAction]:
        out: list[TunerAction] = []
        for c in self._controllers:
            out.extend(c.propose(summary, history, registry=registry))
        return out


class HeuristicProposalController(ProposalController):
    """Hand-tuned heuristics for VM sysctl tuners."""

    def __init__(
        self,
        low_mem_ratio: float = 0.15,
        high_mem_ratio: float = 0.35,
        high_dirty_kb: float = 50_000.0,
        low_dirty_kb: float = 1_000.0,
    ) -> None:
        self.low_mem_ratio = low_mem_ratio
        self.high_mem_ratio = high_mem_ratio
        self.high_dirty_kb = high_dirty_kb
        self.low_dirty_kb = low_dirty_kb

    def _read_int(self, sysctl_name: str) -> int | None:
        try:
            return int(read_sysctl(sysctl_name_to_path(sysctl_name), "int"))
        except OSError:
            return None

    def propose(
        self,
        summary: dict[str, Any],
        history: list[dict[str, Any]],
        *,
        registry: TunerRegistry,
    ) -> list[TunerAction]:
        del history
        host = summary.get("host_features", {})
        mem_avail = float(host.get("host_mem_available_ratio", 1.0))
        swap_free = float(host.get("host_swap_free_ratio", 1.0))
        dirty_kb  = float(host.get("host_dirty_kb", 0.0))
        actions: list[TunerAction] = []

        # vm.swappiness
        if registry.is_enabled("sysctl_vm_swappiness"):
            tuner = registry.get("sysctl_vm_swappiness")
            if tuner is not None and tuner.supports(summary):
                entry = tuner._entry
                current = self._read_int("vm.swappiness")
                if current is not None and entry.min_value is not None and entry.max_value is not None:
                    if mem_avail <= self.low_mem_ratio and swap_free > 0.2 and current < entry.max_value:
                        actions.append(TunerAction(
                            tuner_id="sysctl_vm_swappiness",
                            action_id="increase_swappiness",
                            target="vm.swappiness",
                            value=min(int(entry.max_value), current + int(entry.step)),
                            reason="Low memory; bias reclaim toward swap.",
                            priority=50,
                            metadata={"current": current, "mem_available_ratio": mem_avail},
                        ))
                    elif mem_avail >= self.high_mem_ratio and current > entry.min_value:
                        actions.append(TunerAction(
                            tuner_id="sysctl_vm_swappiness",
                            action_id="decrease_swappiness",
                            target="vm.swappiness",
                            value=max(int(entry.min_value), current - int(entry.step)),
                            reason="Healthy free memory; reduce swap aggressiveness.",
                            priority=40,
                            metadata={"current": current, "mem_available_ratio": mem_avail},
                        ))

        # vm.dirty_ratio
        if registry.is_enabled("sysctl_vm_dirty_ratio"):
            tuner = registry.get("sysctl_vm_dirty_ratio")
            if tuner is not None and tuner.supports(summary):
                entry = tuner._entry
                current = self._read_int("vm.dirty_ratio")
                if current is not None and entry.min_value is not None and entry.max_value is not None:
                    if dirty_kb > self.high_dirty_kb and current > entry.min_value:
                        actions.append(TunerAction(
                            tuner_id="sysctl_vm_dirty_ratio",
                            action_id="decrease_dirty_ratio",
                            target="vm.dirty_ratio",
                            value=max(int(entry.min_value), current - int(entry.step)),
                            reason="High dirty memory; trigger writeback sooner.",
                            priority=45,
                            metadata={"current": current, "dirty_kb": dirty_kb},
                        ))
                    elif dirty_kb < self.low_dirty_kb and current < entry.max_value:
                        actions.append(TunerAction(
                            tuner_id="sysctl_vm_dirty_ratio",
                            action_id="increase_dirty_ratio",
                            target="vm.dirty_ratio",
                            value=min(int(entry.max_value), current + int(entry.step)),
                            reason="Low dirty memory; allow more buffering before writeback.",
                            priority=35,
                            metadata={"current": current, "dirty_kb": dirty_kb},
                        ))

        # vm.vfs_cache_pressure
        if registry.is_enabled("sysctl_vm_vfs_cache_pressure"):
            tuner = registry.get("sysctl_vm_vfs_cache_pressure")
            if tuner is not None and tuner.supports(summary):
                entry = tuner._entry
                current = self._read_int("vm.vfs_cache_pressure")
                if current is not None and entry.min_value is not None and entry.max_value is not None:
                    if mem_avail < 0.20 and current < entry.max_value:
                        actions.append(TunerAction(
                            tuner_id="sysctl_vm_vfs_cache_pressure",
                            action_id="increase_cache_pressure",
                            target="vm.vfs_cache_pressure",
                            value=min(int(entry.max_value), current + int(entry.step)),
                            reason="Low memory; reclaim inode/dentry cache more aggressively.",
                            priority=48,
                            metadata={"current": current, "mem_available_ratio": mem_avail},
                        ))
                    elif mem_avail > 0.50 and current > entry.min_value:
                        actions.append(TunerAction(
                            tuner_id="sysctl_vm_vfs_cache_pressure",
                            action_id="decrease_cache_pressure",
                            target="vm.vfs_cache_pressure",
                            value=max(int(entry.min_value), current - int(entry.step)),
                            reason="Healthy memory; keep inode/dentry cache warmer.",
                            priority=38,
                            metadata={"current": current, "mem_available_ratio": mem_avail},
                        ))

        return actions


class ExternalJsonlProposalController(ProposalController):
    """Reads newline-delimited JSON actions appended by an external process."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._offset = 0
        if path.is_file():
            self._offset = path.stat().st_size

    def propose(
        self,
        summary: dict[str, Any],
        history: list[dict[str, Any]],
        *,
        registry: TunerRegistry,
    ) -> list[TunerAction]:
        del summary, history
        if not self._path.is_file():
            return []
        try:
            text = self._path.read_text(encoding="utf-8")
        except OSError:
            return []
        if len(text) < self._offset:
            self._offset = 0
        chunk = text[self._offset :]
        self._offset = len(text)
        actions: list[TunerAction] = []
        for line in chunk.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                raw = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(raw, dict):
                continue
            try:
                tid = str(raw["tuner_id"])
                action = TunerAction(
                    tuner_id=tid,
                    action_id=str(raw.get("action_id", "external")),
                    target=str(raw["target"]),
                    value=raw["value"],
                    reason=str(raw.get("reason", "")),
                    priority=int(raw.get("priority", 0)),
                    metadata=raw.get("metadata") if isinstance(raw.get("metadata"), dict) else {},
                )
            except (KeyError, TypeError, ValueError):
                continue
            if registry.get(tid) is None:
                continue
            actions.append(action)
        return actions
