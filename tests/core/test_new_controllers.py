from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from reflex.core import AggregatorSample
from reflex.core.tuners.schema import TunerCatalogEntry
from reflex.core.tuners.sysctl import GenericSysctlTuner
from reflex.core.tuners.registry import TunerRegistry
from reflex.implementations.controllers.bandit import ContextualBanditController
from reflex.implementations.controllers.hillclimb import HillClimbController
from reflex.implementations.controllers.openai import OpenAITuningController
from reflex.implementations.controllers.tuning_shared import (
    build_step_candidate,
    compute_reward,
    eligible_tuners,
    load_reward_metrics,
)


class FakeContext:
    def __init__(self) -> None:
        self.decisions: list[tuple[str, str, dict[str, Any]]] = []
        self.executors: list[Any] = []
        self.trigger = type("Trigger", (), {"id": 1, "reason": "test", "metadata": {}})()

    async def log_decision(
        self,
        kind: str,
        message: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        self.decisions.append((kind, message, metadata or {}))

    async def enqueue_executor(
        self,
        executor: Any,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        self.executors.append((executor, metadata or {}))

    def executor_queue_size(self) -> int:
        return 0


class FakeResponses:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload

    def create(self, **kwargs: Any) -> Any:
        del kwargs
        return self.payload


class FakeOpenAIClient:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.responses = FakeResponses(payload)


def _registry(tmp_path: Path, *, initial_value: int = 60) -> TunerRegistry:
    sysctl_dir = tmp_path / "vm"
    sysctl_dir.mkdir(exist_ok=True)
    (sysctl_dir / "swappiness").write_text(f"{initial_value}\n", encoding="utf-8")
    entry = TunerCatalogEntry(
        id="sysctl_vm_swappiness",
        category="vm",
        description="swappiness",
        kind="int",
        sysctl="vm.swappiness",
        min_value=0,
        max_value=100,
        step=5,
    )
    return TunerRegistry([GenericSysctlTuner(entry, sysctl_root=tmp_path)])


def _summary(mem: float = 0.5, latency: float = 1000.0) -> dict[str, Any]:
    return {
        "metrics": {
            "rq_latency_p95_us": latency,
            "direct_reclaim_rate_per_sec": 0.0,
        },
        "host_features": {
            "host_mem_available_ratio": mem,
            "host_cpu_busy_ratio": 0.2,
            "host_dirty_kb": 1000.0,
        },
    }


def test_reward_uses_direction_and_normalization() -> None:
    metrics = load_reward_metrics(Path("configs/reward_weights.yaml"))
    reward = compute_reward(_summary(mem=1.0, latency=0.0), metrics)

    assert reward.total_reward > 0.9
    assert reward.per_metric_terms["mem_avail"]["direction"] == "maximize"
    assert reward.per_metric_terms["rq_latency"]["direction"] == "minimize"


def test_action_generation_clamps_and_noops_on_unsafe_read(tmp_path: Path) -> None:
    registry = _registry(tmp_path, initial_value=98)
    summary = _summary()
    tuners = eligible_tuners(registry, summary)

    candidate = build_step_candidate(tuners[0], "increase", reason="test")

    assert candidate.action is not None
    assert candidate.action.value == 100


def test_openai_validation_schedules_only_when_apply_allowed(tmp_path: Path) -> None:
    async def scenario() -> None:
        registry = _registry(tmp_path)
        payload = {
            "actions": [
                {
                    "tuner_id": "sysctl_vm_swappiness",
                    "target": "vm.swappiness",
                    "direction": "increase",
                    "steps": 1,
                    "confidence": 0.7,
                    "reason": "test proposal",
                }
            ]
        }
        controller = OpenAITuningController(
            registry,
            allow_apply=False,
            client=FakeOpenAIClient(payload),
        )
        await controller.accept_data(AggregatorSample(1, 0.0, _summary()))
        ctx = FakeContext()
        await controller.run(ctx)

        assert ctx.decisions[-1][2]["action"] == "increase_sysctl_vm_swappiness"
        assert ctx.executors == []

    asyncio.run(scenario())


def test_hillclimb_exposes_pending_action_and_blocks_overlap(tmp_path: Path) -> None:
    async def scenario() -> None:
        registry = _registry(tmp_path)
        controller = HillClimbController(
            registry,
            interval_windows=1,
            evaluate_after_windows=3,
            baseline_windows=1,
            epsilon=0.0,
        )
        await controller.accept_data(AggregatorSample(1, 0.0, _summary()))
        ctx = FakeContext()
        await controller.run(ctx)
        await controller.accept_data(AggregatorSample(2, 0.0, _summary()))
        await controller.run(ctx)

        assert len(ctx.executors) == 1
        assert ctx.decisions[-1][2]["pending_action"]["evaluation_due_at_sample_id"] == 4
        assert "awaiting evaluation" in ctx.decisions[-1][1]

    asyncio.run(scenario())


def test_hillclimb_accepted_evaluation_logs_noop_not_rollback(tmp_path: Path) -> None:
    async def scenario() -> None:
        registry = _registry(tmp_path)
        controller = HillClimbController(
            registry,
            interval_windows=1,
            evaluate_after_windows=1,
            baseline_windows=1,
            epsilon=0.0,
        )
        ctx = FakeContext()
        await controller.accept_data(AggregatorSample(1, 0.0, _summary()))
        await controller.run(ctx)
        await controller.accept_data(AggregatorSample(2, 0.0, _summary(mem=0.9, latency=100.0)))
        await controller.run(ctx)

        metadata = ctx.decisions[-1][2]
        assert "evaluation complete" in ctx.decisions[-1][1]
        assert metadata["accepted"] is True
        assert metadata["action"] == "noop"
        assert metadata["reason"] == "accepted candidate"
        assert len(ctx.executors) == 1

    asyncio.run(scenario())


def test_bandit_learns_delta_not_absolute_reward_and_cools_negative_action(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        registry = _registry(tmp_path)
        controller = ContextualBanditController(
            registry,
            alpha=1.0,
            epsilon=0.0,
            evaluate_after_windows=1,
            baseline_windows=1,
            library_path=tmp_path / "missing.json",
            negative_cooldown_windows=4,
        )
        ctx = FakeContext()
        await controller.accept_data(AggregatorSample(1, 0.0, _summary(mem=1.0, latency=0.0)))
        await controller.run(ctx)
        await controller.accept_data(AggregatorSample(2, 0.0, _summary(mem=0.1, latency=9000.0)))
        await controller.run(ctx)

        metadata = ctx.decisions[-1][2]
        assert metadata["accepted"] is False
        assert metadata["target_delta"] < 0.0
        assert metadata["prediction_after_update"] < 0.0
        assert metadata["banned_until"] == 6

    asyncio.run(scenario())
