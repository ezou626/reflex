from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Any


@dataclass
class WindowComparison:
    previous: dict[str, float]
    current: dict[str, float]
    delta: dict[str, float]


class WindowHistory:
    def __init__(self, size: int = 60) -> None:
        self._summaries: deque[dict[str, Any]] = deque(maxlen=size)

    def add(self, summary: dict[str, Any]) -> None:
        self._summaries.append(summary)

    def latest(self, n: int) -> list[dict[str, Any]]:
        if n <= 0:
            return []
        return list(self._summaries)[-n:]

    def compare_last_two(self, keys: list[str]) -> WindowComparison | None:
        if len(self._summaries) < 2:
            return None
        prev = self._summaries[-2]
        curr = self._summaries[-1]
        prev_metrics = _extract_metrics(prev, keys)
        curr_metrics = _extract_metrics(curr, keys)
        delta = {k: curr_metrics[k] - prev_metrics[k] for k in prev_metrics}
        return WindowComparison(previous=prev_metrics, current=curr_metrics, delta=delta)


def _extract_metrics(summary: dict[str, Any], keys: list[str]) -> dict[str, float]:
    metrics = summary.get("metrics", {})
    host = summary.get("host_features", {})
    out: dict[str, float] = {}
    for key in keys:
        if key in metrics:
            out[key] = float(metrics[key])
        elif key in host:
            out[key] = float(host[key])
        else:
            out[key] = 0.0
    return out
