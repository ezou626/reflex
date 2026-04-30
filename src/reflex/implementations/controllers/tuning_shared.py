from __future__ import annotations

import json
import math
import random
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal

import yaml

from reflex.core.tuners import TunerAction, TunerRegistry
from reflex.core.tuners.sysctl_util import read_sysctl, sysctl_name_to_path
from reflex.core.types import AggregatorSample

Direction = Literal["increase", "decrease"]


LEGACY_REWARD_METRICS: dict[str, tuple[str, str, float]] = {
    "mem_avail": ("host_mem_available_ratio", "maximize", 1.0),
    "rq_latency": ("rq_latency_p95_us", "minimize", 10_000.0),
    "direct_reclaim": ("direct_reclaim_rate_per_sec", "minimize", 100.0),
    "cpu_busy": ("host_cpu_busy_ratio", "minimize", 1.0),
    "dirty": ("host_dirty_kb", "minimize", 200_000.0),
}


@dataclass(frozen=True)
class RewardMetric:
    name: str
    key: str
    direction: Literal["maximize", "minimize"]
    weight: float
    scale: float = 1.0


@dataclass(frozen=True)
class RewardResult:
    total_reward: float
    per_metric_terms: dict[str, dict[str, float | str]]


@dataclass(frozen=True)
class EligibleTuner:
    tuner_id: str
    target: str
    description: str
    min_value: int
    max_value: int
    step: int
    tuner: Any


@dataclass(frozen=True)
class ActionCandidate:
    action: TunerAction | None
    direction: Direction | Literal["noop"]
    current_value: int | None
    candidate_value: int | None
    reason: str
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def action_name(self) -> str:
        if self.action is None:
            return "noop"
        return self.action.action_id


@dataclass
class PendingAction:
    tuner_id: str | None
    action: str
    prev_value: int | None
    new_value: int | None
    reward_before: float
    applied_at_sample_id: int
    evaluation_due_at_sample_id: int
    rollback_action: TunerAction | None = None

    def log_payload(self) -> dict[str, Any]:
        return {
            "tuner_id": self.tuner_id,
            "action": self.action,
            "prev_value": self.prev_value,
            "new_value": self.new_value,
            "reward_before": self.reward_before,
            "applied_at_sample_id": self.applied_at_sample_id,
            "evaluation_due_at_sample_id": self.evaluation_due_at_sample_id,
        }


def repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def default_reward_path() -> Path:
    return repo_root() / "configs" / "reward_weights.yaml"


def summary_from_sample(sample: AggregatorSample) -> dict[str, Any] | None:
    if isinstance(sample.sample, dict):
        return sample.sample
    return None


def summary_metric(summary: dict[str, Any], key: str) -> float:
    for section in ("metrics", "host_features"):
        value = summary.get(section, {}).get(key)
        if value is not None:
            return float(value)
    value = summary.get(key)
    return float(value) if value is not None else 0.0


def load_reward_metrics(path: Path | None = None) -> list[RewardMetric]:
    path = path or default_reward_path()
    if not path.is_file():
        return []
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        return []

    metrics_raw = raw.get("metrics")
    if isinstance(metrics_raw, list):
        out: list[RewardMetric] = []
        for item in metrics_raw:
            if not isinstance(item, dict):
                continue
            direction = str(item.get("direction", "maximize"))
            if direction not in ("maximize", "minimize"):
                continue
            out.append(
                RewardMetric(
                    name=str(item.get("name", item.get("key", ""))),
                    key=str(item.get("key", item.get("name", ""))),
                    direction=direction,  # type: ignore[arg-type]
                    weight=float(item.get("weight", 0.0)),
                    scale=max(float(item.get("scale", 1.0)), 1e-9),
                )
            )
        return out

    out = []
    for name, weight in raw.items():
        if name not in LEGACY_REWARD_METRICS or not isinstance(weight, (int, float)):
            continue
        key, direction, scale = LEGACY_REWARD_METRICS[name]
        out.append(
            RewardMetric(
                name=name,
                key=key,
                direction=direction,  # type: ignore[arg-type]
                weight=abs(float(weight)),
                scale=scale,
            )
        )
    return out


def compute_reward(summary: dict[str, Any], metrics: list[RewardMetric]) -> RewardResult:
    total = 0.0
    terms: dict[str, dict[str, float | str]] = {}
    for metric in metrics:
        raw = summary_metric(summary, metric.key)
        normalized = max(0.0, min(raw / metric.scale, 1.0))
        score = normalized if metric.direction == "maximize" else 1.0 - normalized
        term = metric.weight * score
        total += term
        terms[metric.name] = {
            "key": metric.key,
            "direction": metric.direction,
            "raw": raw,
            "normalized": normalized,
            "weight": metric.weight,
            "term": term,
        }
    return RewardResult(total_reward=total, per_metric_terms=terms)


def smoothed_reward(
    summaries: list[dict[str, Any]],
    metrics: list[RewardMetric],
    reward_window: int,
) -> RewardResult:
    window = summaries[-max(1, reward_window) :]
    if not window:
        return RewardResult(0.0, {})
    results = [compute_reward(summary, metrics) for summary in window]
    total = sum(r.total_reward for r in results) / len(results)
    terms: dict[str, dict[str, float | str]] = {}
    for metric in metrics:
        vals = [float(r.per_metric_terms.get(metric.name, {}).get("term", 0.0)) for r in results]
        terms[metric.name] = {
            "key": metric.key,
            "direction": metric.direction,
            "weight": metric.weight,
            "term": sum(vals) / len(vals),
        }
    return RewardResult(total_reward=total, per_metric_terms=terms)


def eligible_tuners(registry: TunerRegistry, summary: dict[str, Any]) -> list[EligibleTuner]:
    out: list[EligibleTuner] = []
    for tuner in registry.enabled_tuners():
        entry = getattr(tuner, "_entry", None)
        if entry is None:
            continue
        if entry.scope != "runtime_sysctl" or entry.kind != "int":
            continue
        if entry.min_value is None or entry.max_value is None or entry.step is None:
            continue
        if int(entry.step) <= 0:
            continue
        try:
            if not tuner.supports(summary):
                continue
        except OSError:
            continue
        out.append(
            EligibleTuner(
                tuner_id=entry.id,
                target=entry.sysctl,
                description=entry.description,
                min_value=int(entry.min_value),
                max_value=int(entry.max_value),
                step=int(entry.step),
                tuner=tuner,
            )
        )
    return out


def read_current_value(tuner: EligibleTuner) -> int | None:
    try:
        return int(read_sysctl(sysctl_name_to_path(tuner.target), "int"))
    except (OSError, ValueError, TypeError):
        return None


def current_values(tuners: list[EligibleTuner]) -> dict[str, int]:
    values: dict[str, int] = {}
    for tuner in tuners:
        current = read_current_value(tuner)
        if current is not None:
            values[tuner.tuner_id] = current
    return values


def clamp_to_step(value: int, tuner: EligibleTuner) -> int:
    clamped = max(tuner.min_value, min(tuner.max_value, int(value)))
    steps = round((clamped - tuner.min_value) / tuner.step)
    stepped = tuner.min_value + steps * tuner.step
    return max(tuner.min_value, min(tuner.max_value, int(stepped)))


def build_step_candidate(
    tuner: EligibleTuner,
    direction: Direction,
    *,
    steps: int = 1,
    reason: str,
    priority: int = 50,
) -> ActionCandidate:
    current = read_current_value(tuner)
    if current is None:
        return noop_candidate("unsafe read", {"tuner_id": tuner.tuner_id})
    delta = tuner.step * max(1, steps)
    raw = current + delta if direction == "increase" else current - delta
    candidate = clamp_to_step(raw, tuner)
    if candidate == current:
        return noop_candidate("boundary reached", {"tuner_id": tuner.tuner_id, "current": current})
    action = TunerAction(
        tuner_id=tuner.tuner_id,
        action_id=f"{direction}_{tuner.tuner_id}",
        target=tuner.target,
        value=candidate,
        reason=reason,
        priority=priority,
        metadata={"current": current, "direction": direction, "steps": steps},
    )
    return ActionCandidate(
        action=action,
        direction=direction,
        current_value=current,
        candidate_value=candidate,
        reason=reason,
    )


def build_set_action(
    tuner: EligibleTuner,
    value: int,
    *,
    action_id: str,
    reason: str,
    priority: int = 80,
) -> TunerAction:
    return TunerAction(
        tuner_id=tuner.tuner_id,
        action_id=action_id,
        target=tuner.target,
        value=clamp_to_step(value, tuner),
        reason=reason,
        priority=priority,
    )


def noop_candidate(reason: str, metadata: dict[str, Any] | None = None) -> ActionCandidate:
    return ActionCandidate(
        action=None,
        direction="noop",
        current_value=None,
        candidate_value=None,
        reason=reason,
        metadata=metadata or {},
    )


def generic_action_space(tuners: list[EligibleTuner]) -> list[ActionCandidate]:
    actions: list[ActionCandidate] = [noop_candidate("no improvement expected")]
    for tuner in tuners:
        actions.append(build_step_candidate(tuner, "increase", reason="generic one-step increase"))
        actions.append(build_step_candidate(tuner, "decrease", reason="generic one-step decrease"))
    return actions


def canonical_decision_metadata(
    *,
    controller: str,
    candidate: ActionCandidate,
    reward_before: RewardResult | None = None,
    reward_after: RewardResult | None = None,
    accepted: bool | None = None,
    pending_action: PendingAction | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    before = reward_before.total_reward if reward_before is not None else None
    after = reward_after.total_reward if reward_after is not None else None
    metadata: dict[str, Any] = {
        "controller": controller,
        "action": candidate.action_name,
        "reason": candidate.reason,
        "tuner_id": candidate.action.tuner_id if candidate.action is not None else None,
        "prev_value": candidate.current_value,
        "new_value": candidate.candidate_value,
        "reward_before": before,
        "reward_after": after,
        "delta": (after - before) if before is not None and after is not None else None,
        "accepted": accepted,
        "total_reward": after if after is not None else before,
        "per_metric_terms": (
            reward_after.per_metric_terms
            if reward_after is not None
            else reward_before.per_metric_terms
            if reward_before is not None
            else {}
        ),
        "pending_action": pending_action.log_payload() if pending_action is not None else None,
    }
    metadata.update(extra or {})
    return metadata


def dump_state(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, default=_json_default), encoding="utf-8")


def load_state(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return raw if isinstance(raw, dict) else {}


def _json_default(value: Any) -> Any:
    if hasattr(value, "__dataclass_fields__"):
        return asdict(value)
    return str(value)


def annealing_accept(delta: float, temperature: float, rng: random.Random) -> bool:
    if temperature <= 0.0 or delta >= 0.0:
        return delta >= 0.0
    return rng.random() < math.exp(delta / temperature)
