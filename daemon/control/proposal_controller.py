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
    """Hand-tuned heuristics (e.g. swappiness vs memory pressure)."""

    def __init__(
        self,
        min_swappiness: int = 10,
        max_swappiness: int = 90,
        step: int = 5,
        low_mem_ratio: float = 0.15,
        high_mem_ratio: float = 0.35,
    ) -> None:
        self.min_swappiness = min_swappiness
        self.max_swappiness = max_swappiness
        self.step = step
        self.low_mem_ratio = low_mem_ratio
        self.high_mem_ratio = high_mem_ratio

    def propose(
        self,
        summary: dict[str, Any],
        history: list[dict[str, Any]],
        *,
        registry: TunerRegistry,
    ) -> list[TunerAction]:
        del history
        if not registry.is_enabled("sysctl_vm_swappiness"):
            return []
        tuner = registry.get("sysctl_vm_swappiness")
        if tuner is None or not tuner.supports(summary):
            return []
        host = summary.get("host_features", {})
        if "host_mem_available_ratio" not in host:
            return []
        mem_avail = float(host.get("host_mem_available_ratio", 1.0))
        swap_free = float(host.get("host_swap_free_ratio", 1.0))
        path = sysctl_name_to_path("vm.swappiness")
        try:
            current = int(read_sysctl(path, "int"))
        except OSError:
            return []
        actions: list[TunerAction] = []
        if mem_avail <= self.low_mem_ratio and swap_free > 0.2 and current < self.max_swappiness:
            new_value = min(self.max_swappiness, current + self.step)
            actions.append(
                TunerAction(
                    tuner_id="sysctl_vm_swappiness",
                    action_id="increase_swappiness",
                    target="vm.swappiness",
                    value=new_value,
                    reason="Low memory availability; bias reclaim toward swap.",
                    priority=50,
                    metadata={"current": current, "mem_available_ratio": mem_avail},
                )
            )
        elif mem_avail >= self.high_mem_ratio and current > self.min_swappiness:
            new_value = max(self.min_swappiness, current - self.step)
            actions.append(
                TunerAction(
                    tuner_id="sysctl_vm_swappiness",
                    action_id="decrease_swappiness",
                    target="vm.swappiness",
                    value=new_value,
                    reason="Healthy free memory; reduce swap aggressiveness.",
                    priority=40,
                    metadata={"current": current, "mem_available_ratio": mem_avail},
                )
            )
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
