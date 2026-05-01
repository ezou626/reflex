from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any

from reflex.core.tuners import TunerRegistry
from reflex.core.types import AggregatorSample, ControllerRunContext
from reflex.implementations.controllers.tuning_shared import (
    ActionCandidate,
    build_step_candidate,
    current_values,
    eligible_tuners,
    noop_candidate,
    summary_from_sample,
)
from reflex.implementations.executors import BatchTunerExecutor


OPENAI_ACTION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "actions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "tuner_id": {"type": "string"},
                    "target": {"type": "string"},
                    "direction": {"type": "string", "enum": ["increase", "decrease"]},
                    "steps": {"type": "integer"},
                    "confidence": {"type": "number"},
                    "reason": {"type": "string"},
                },
                "required": [
                    "tuner_id",
                    "target",
                    "direction",
                    "steps",
                    "confidence",
                    "reason",
                ],
                "additionalProperties": False,
            },
        }
    },
    "required": ["actions"],
    "additionalProperties": False,
}


class OpenAITuningController:
    def __init__(
        self,
        registry: TunerRegistry,
        *,
        model: str = "gpt-5-mini",
        max_actions: int = 1,
        history_windows: int = 5,
        timeout_sec: float = 15.0,
        allow_apply: bool = False,
        reward_path: Path | None = None,
        reward_window: int = 3,
        max_steps_per_run: int = 1,
        client: Any | None = None,
        max_history: int = 60,
    ) -> None:
        self.registry = registry
        self.model = model
        self.max_actions = max(1, min(max_actions, 1))
        self.history_windows = max(1, history_windows)
        self.timeout_sec = timeout_sec
        self.allow_apply = allow_apply
        del reward_path
        del reward_window
        self.max_steps_per_run = max(1, max_steps_per_run)
        self.client = client
        self.max_history = max_history
        self.history: list[tuple[int, dict[str, Any]]] = []

    async def accept_data(self, sample: AggregatorSample) -> None:
        summary = summary_from_sample(sample)
        if summary is None:
            return
        self.history.append((sample.id, summary))
        self.history = self.history[-self.max_history :]

    def _summaries(self) -> list[dict[str, Any]]:
        return [summary for _, summary in self.history]

    def _current_summary(self) -> dict[str, Any]:
        return self.history[-1][1]

    @staticmethod
    def _decision_signals_from_summary(summary: dict[str, Any]) -> dict[str, Any]:
        metrics = summary.get("metrics", {})
        host = summary.get("host_features", {})
        return {
            "window_sec": summary.get("window_sec"),
            "syscall_latency_p95_us": metrics.get("syscall_latency_p95_us"),
            "syscall_latency_count": metrics.get("syscall_latency_count"),
            "syscall_error_rate": metrics.get("syscall_error_rate"),
            "syscall_error_rate_per_sec": metrics.get("syscall_error_rate_per_sec"),
            "rq_latency_p95_us": metrics.get("rq_latency_p95_us"),
            "blk_latency_p95_us": metrics.get("blk_latency_p95_us"),
            "context_switch_rate_per_sec": metrics.get("context_switch_rate_per_sec"),
            "direct_reclaim_rate_per_sec": metrics.get("direct_reclaim_rate_per_sec"),
            "process_churn_rate_per_sec": metrics.get("process_churn_rate_per_sec"),
            "host_mem_available_ratio": host.get("host_mem_available_ratio"),
            "host_swap_free_ratio": host.get("host_swap_free_ratio"),
            "host_cpu_util_pct": host.get("host_cpu_util_pct"),
            "host_cpu_iowait_pct": host.get("host_cpu_iowait_pct"),
            "host_load_per_cpu": host.get("host_load_per_cpu"),
            "host_dirty_kb": host.get("host_dirty_kb"),
        }

    def _decision_signal_history(self) -> list[dict[str, Any]]:
        return [
            self._decision_signals_from_summary(summary)
            for summary in self._summaries()[-self.history_windows :]
        ]

    def _catalog_payload(self) -> list[dict[str, Any]]:
        tuners = eligible_tuners(self.registry)
        tuners = self._filter_tuners_by_bottleneck(tuners)
        values = current_values(tuners)
        return [
            {
                "tuner_id": tuner.tuner_id,
                "target": tuner.target,
                "description": tuner.description,
                "min_value": tuner.min_value,
                "max_value": tuner.max_value,
                "step": tuner.step,
                "current_value": values.get(tuner.tuner_id),
            }
            for tuner in tuners
            if tuner.tuner_id in values
        ]

    def _filter_tuners_by_bottleneck(self, tuners: list[Any]) -> list[Any]:
        summary = self._current_summary() if self.history else {}
        host = summary.get("host_features", {})
        metrics = summary.get("metrics", {})
        mem_avail = float(host.get("host_mem_available_ratio", 1.0))
        swap_free = float(host.get("host_swap_free_ratio", 1.0))
        direct_reclaim_rate = float(metrics.get("direct_reclaim_rate_per_sec", 0.0))
        memory_bottleneck = (
            mem_avail <= 0.15
            or swap_free <= 0.20
            or direct_reclaim_rate >= 1.0
        )
        if memory_bottleneck:
            return tuners
        filtered: list[Any] = []
        for tuner in tuners:
            category = getattr(getattr(tuner, "tuner", None), "_entry", None)
            if getattr(category, "category", None) == "vm":
                continue
            filtered.append(tuner)
        return filtered

    async def run(self, ctx: ControllerRunContext) -> None:
        if not self.history:
            await ctx.log_decision("openai", "no summaries available", {})
            await ctx.record_execution_result(
                ok=False,
                error="no summaries available",
                payload={"controller": "openai", "outcome": "noop"},
            )
            return
        if not os.environ.get("OPENAI_API_KEY") and self.client is None:
            await ctx.log_decision(
                "openai",
                "OPENAI_API_KEY missing; no-op",
                _openai_decision_metadata(noop_candidate("missing api key")),
            )
            await ctx.record_execution_result(
                ok=False,
                error="OPENAI_API_KEY missing",
                payload={"controller": "openai", "outcome": "noop"},
            )
            return
        client = self.client or self._make_client()
        if client is None:
            await ctx.log_decision(
                "openai",
                "OpenAI SDK unavailable; no-op",
                _openai_decision_metadata(noop_candidate("openai sdk unavailable")),
            )
            await ctx.record_execution_result(
                ok=False,
                error="OpenAI SDK unavailable",
                payload={"controller": "openai", "outcome": "noop"},
            )
            return
        catalog = self._catalog_payload()
        try:
            raw = await asyncio.to_thread(self._request, client, catalog)
        except Exception as exc:  # pragma: no cover - defensive boundary for SDK failures
            await ctx.log_decision(
                "openai",
                f"OpenAI request failed: {exc}",
                _openai_decision_metadata(
                    noop_candidate("openai request failed"),
                    {"error_type": type(exc).__name__},
                ),
            )
            await ctx.record_execution_result(
                ok=False,
                error=f"OpenAI request failed: {type(exc).__name__}: {exc}",
                payload={"controller": "openai", "outcome": "request_failed"},
            )
            return
        candidates = self._validate_response(raw)
        selected = candidates[: self.max_actions]
        if not selected:
            await ctx.log_decision(
                "openai",
                "OpenAI proposed no valid actions",
                _openai_decision_metadata(
                    noop_candidate("no valid openai actions"),
                    {
                        "raw_response": raw,
                        "eligible_tuners": len(catalog),
                        "tuner_ids": [tuner["tuner_id"] for tuner in catalog],
                    },
                ),
            )
            await ctx.record_execution_result(
                ok=False,
                error="OpenAI proposed no valid actions",
                payload={"controller": "openai", "outcome": "no_valid_actions"},
            )
            return
        candidate = selected[0]
        await ctx.log_decision(
            "openai",
            "OpenAI proposal validated",
            _openai_decision_metadata(
                candidate,
                {
                    "allow_apply": self.allow_apply,
                    "raw_response": raw,
                    "eligible_tuners": len(catalog),
                    "tuner_ids": [tuner["tuner_id"] for tuner in catalog],
                },
            ),
        )
        if self.allow_apply and candidate.action is not None:
            await ctx.enqueue_executor(
                BatchTunerExecutor(self.registry, [candidate.action]),
                {
                    "controller": "openai",
                    "action_count": 1,
                    "tuner_ids": [candidate.action.tuner_id],
                },
            )
            return

        await ctx.record_execution_result(
            ok=True,
            payload={
                "controller": "openai",
                "outcome": "proposal_validated_not_applied",
                "allow_apply": self.allow_apply,
                "action": candidate.action_name,
                "reason": candidate.reason,
                "direction": candidate.direction,
                "current_value": candidate.current_value,
                "candidate_value": candidate.candidate_value,
                "metadata": candidate.metadata,
            },
        )

    def _make_client(self) -> Any | None:
        try:
            from openai import OpenAI  # type: ignore[import-not-found]
        except ImportError:
            return None
        return OpenAI(timeout=self.timeout_sec)

    def _request(self, client: Any, catalog: list[dict[str, Any]]) -> dict[str, Any]:
        prompt = {
            "latest_summary": self._current_summary(),
            "history": self._summaries()[-self.history_windows :],
            "latest_decision_signals": self._decision_signals_from_summary(
                self._current_summary()
            ),
            "decision_signal_history": self._decision_signal_history(),
            "tuners": catalog,
            "instruction": (
                "Return JSON actions only. Propose exactly one conservative one-step "
                "sysctl change from the provided tuner catalog when a safe improvement "
                "is plausible. Return an empty actions list only when the tuner catalog "
                "is empty or every available action is unsafe or likely harmful. "
                "Treat syscall_latency_p95_us and syscall_error_rate as first-class "
                "workload health signals, alongside rq/block latency, reclaim, "
                "context-switch rate, and host memory/CPU pressure. "
                "Prefer no-op unless a specific bottleneck is clear. "
                "Do not tune a subsystem merely because it has headroom. "
            ),
        }
        response = client.responses.create(
            model=self.model,
            input=[
                {
                    "role": "system",
                    "content": "You propose conservative Linux sysctl tuning actions as strict JSON.",
                },
                {"role": "user", "content": json.dumps(prompt, default=str)},
            ],
            text={
                "format": {
                    "type": "json_schema",
                    "name": "reflex_tuning_actions",
                    "strict": True,
                    "schema": OPENAI_ACTION_SCHEMA,
                }
            },
        )
        return _extract_json_response(response)

    def _validate_response(self, raw: dict[str, Any]) -> list[ActionCandidate]:
        actions = raw.get("actions", [])
        if not isinstance(actions, list):
            return []
        tuners = {t.tuner_id: t for t in eligible_tuners(self.registry)}
        out: list[ActionCandidate] = []
        for item in actions:
            if not isinstance(item, dict):
                continue
            tuner_id = str(item.get("tuner_id", ""))
            tuner = tuners.get(tuner_id)
            if tuner is None:
                continue
            if str(item.get("target", "")) != tuner.target:
                continue
            direction = item.get("direction")
            if direction not in ("increase", "decrease"):
                continue
            try:
                steps = max(1, min(int(item.get("steps", 1)), self.max_steps_per_run))
            except (TypeError, ValueError):
                continue
            candidate = build_step_candidate(
                tuner,
                direction,  # type: ignore[arg-type]
                steps=steps,
                reason=str(item.get("reason", "OpenAI proposal")),
            )
            if candidate.action is not None:
                out.append(candidate)
        return out


def _extract_json_response(response: Any) -> dict[str, Any]:
    parsed = getattr(response, "output_parsed", None)
    if isinstance(parsed, dict):
        return parsed
    text = getattr(response, "output_text", None)
    if isinstance(text, str):
        try:
            raw = json.loads(text)
            return raw if isinstance(raw, dict) else {}
        except json.JSONDecodeError:
            return {}
    output = getattr(response, "output", None)
    if isinstance(output, list):
        for item in output:
            content = getattr(item, "content", None)
            if not isinstance(content, list):
                continue
            for part in content:
                part_text = getattr(part, "text", None)
                if isinstance(part_text, str):
                    try:
                        raw = json.loads(part_text)
                        return raw if isinstance(raw, dict) else {}
                    except json.JSONDecodeError:
                        continue
    if isinstance(response, dict):
        return response
    return {}


def _openai_decision_metadata(
    candidate: ActionCandidate,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    metadata: dict[str, Any] = {
        "controller": "openai",
        "action": candidate.action_name,
        "reason": candidate.reason,
        "tuner_id": candidate.action.tuner_id if candidate.action is not None else None,
        "prev_value": candidate.current_value,
        "new_value": candidate.candidate_value,
    }
    metadata.update(extra or {})
    return metadata


__all__ = ["OpenAITuningController", "OPENAI_ACTION_SCHEMA"]
