from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from daemon_core.tuners import TunerRegistry
from daemon_core.types import AggregatorSample, ControllerRunContext
from implementations.controllers.tuning_shared import (
    ActionCandidate,
    build_step_candidate,
    canonical_decision_metadata,
    current_values,
    default_reward_path,
    eligible_tuners,
    load_reward_metrics,
    noop_candidate,
    smoothed_reward,
    summary_from_sample,
)
from implementations.executors import BatchTunerExecutor


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
        self.reward_metrics = load_reward_metrics(reward_path or default_reward_path())
        self.reward_window = max(1, reward_window)
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

    def _catalog_payload(self) -> list[dict[str, Any]]:
        tuners = eligible_tuners(self.registry, self._current_summary())
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

    async def run(self, ctx: ControllerRunContext) -> None:
        if not self.history:
            await ctx.log_decision("openai", "no summaries available", {})
            return
        reward = smoothed_reward(self._summaries(), self.reward_metrics, self.reward_window)
        if not os.environ.get("OPENAI_API_KEY") and self.client is None:
            await ctx.log_decision(
                "openai",
                "OPENAI_API_KEY missing; no-op",
                canonical_decision_metadata(
                    controller="openai",
                    candidate=noop_candidate("missing api key"),
                    reward_before=reward,
                ),
            )
            return
        client = self.client or self._make_client()
        if client is None:
            await ctx.log_decision(
                "openai",
                "OpenAI SDK unavailable; no-op",
                canonical_decision_metadata(
                    controller="openai",
                    candidate=noop_candidate("openai sdk unavailable"),
                    reward_before=reward,
                ),
            )
            return
        try:
            raw = self._request(client)
        except Exception as exc:  # pragma: no cover - defensive boundary for SDK failures
            await ctx.log_decision(
                "openai",
                f"OpenAI request failed: {exc}",
                canonical_decision_metadata(
                    controller="openai",
                    candidate=noop_candidate("openai request failed"),
                    reward_before=reward,
                    extra={"error_type": type(exc).__name__},
                ),
            )
            return
        candidates = self._validate_response(raw)
        selected = candidates[: self.max_actions]
        if not selected:
            await ctx.log_decision(
                "openai",
                "OpenAI proposed no valid actions",
                canonical_decision_metadata(
                    controller="openai",
                    candidate=noop_candidate("no valid openai actions"),
                    reward_before=reward,
                    extra={"raw_response": raw},
                ),
            )
            return
        candidate = selected[0]
        await ctx.log_decision(
            "openai",
            "OpenAI proposal validated",
            canonical_decision_metadata(
                controller="openai",
                candidate=candidate,
                reward_before=reward,
                extra={"allow_apply": self.allow_apply, "raw_response": raw},
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

    def _make_client(self) -> Any | None:
        try:
            from openai import OpenAI  # type: ignore[import-not-found]
        except ImportError:
            return None
        return OpenAI(timeout=self.timeout_sec)

    def _request(self, client: Any) -> dict[str, Any]:
        prompt = {
            "latest_summary": self._current_summary(),
            "history": self._summaries()[-self.history_windows :],
            "reward": smoothed_reward(
                self._summaries(),
                self.reward_metrics,
                self.reward_window,
            ).__dict__,
            "tuners": self._catalog_payload(),
            "instruction": (
                "Return JSON actions only. Propose at most one safe one-step sysctl "
                "change using the provided tuner catalog. It is acceptable to return "
                "an empty actions list."
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
        tuners = {t.tuner_id: t for t in eligible_tuners(self.registry, self._current_summary())}
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
                priority=int(float(item.get("confidence", 0.0)) * 100),
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


__all__ = ["OpenAITuningController", "OPENAI_ACTION_SCHEMA"]
