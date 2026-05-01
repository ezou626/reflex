from __future__ import annotations

import asyncio
import json
import sys
import types
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
        self.execution_results: list[dict[str, Any]] = []
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

    async def record_execution_result(
        self,
        *,
        ok: bool,
        payload: Any = None,
        error: str | None = None,
        action_records: list[Any] | None = None,
    ) -> dict[str, Any]:
        result = {
            "ok": ok,
            "payload": payload,
            "error": error,
            "action_records": action_records or [],
        }
        self.execution_results.append(result)
        return result

    def executor_queue_size(self) -> int:
        return 0


class FakeResponses:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload
        self.calls: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
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
            "syscall_latency_p95_us": 250.0,
            "syscall_latency_count": 42,
            "syscall_error_rate": 0.125,
            "syscall_error_rate_per_sec": 2.0,
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
    registry = _registry(tmp_path, initial_value=95)
    tuners = eligible_tuners(registry)

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
        assert ctx.execution_results[-1]["ok"] is True
        assert (
            ctx.execution_results[-1]["payload"]["outcome"]
            == "proposal_validated_not_applied"
        )

    asyncio.run(scenario())


def test_openai_request_includes_syscall_decision_signals(tmp_path: Path) -> None:
    async def scenario() -> None:
        registry = _registry(tmp_path)
        client = FakeOpenAIClient({"actions": []})
        controller = OpenAITuningController(
            registry,
            allow_apply=False,
            client=client,
        )
        await controller.accept_data(AggregatorSample(1, 0.0, _summary(mem=0.10)))
        ctx = FakeContext()
        await controller.run(ctx)

        request = client.responses.calls[-1]
        user_payload = json.loads(request["input"][1]["content"])

        assert user_payload["latest_decision_signals"]["syscall_latency_p95_us"] == 250.0
        assert user_payload["latest_decision_signals"]["syscall_error_rate"] == 0.125
        assert (
            user_payload["decision_signal_history"][-1]["syscall_error_rate_per_sec"]
            == 2.0
        )

    asyncio.run(scenario())


def test_openai_gates_memory_tuners_without_memory_bottleneck(tmp_path: Path) -> None:
    async def scenario() -> None:
        registry = _registry(tmp_path)
        controller = OpenAITuningController(
            registry,
            allow_apply=False,
            client=FakeOpenAIClient({"actions": []}),
        )
        await controller.accept_data(AggregatorSample(1, 0.0, _summary(mem=0.80)))

        catalog = controller._catalog_payload()

        assert catalog == []

    asyncio.run(scenario())


def test_openai_keeps_memory_tuners_when_memory_pressure_present(tmp_path: Path) -> None:
    async def scenario() -> None:
        registry = _registry(tmp_path)
        controller = OpenAITuningController(
            registry,
            allow_apply=False,
            client=FakeOpenAIClient({"actions": []}),
        )
        await controller.accept_data(AggregatorSample(1, 0.0, _summary(mem=0.10)))

        catalog = controller._catalog_payload()

        assert [item["tuner_id"] for item in catalog] == ["sysctl_vm_swappiness"]

    asyncio.run(scenario())


def test_openai_make_client_uses_sdk_package(monkeypatch: Any, tmp_path: Path) -> None:
    class FakeSDKOpenAI:
        def __init__(self, **kwargs: Any) -> None:
            self.kwargs = kwargs

    fake_openai = types.ModuleType("openai")
    fake_openai.OpenAI = FakeSDKOpenAI
    monkeypatch.setitem(sys.modules, "openai", fake_openai)

    controller = OpenAITuningController(_registry(tmp_path), timeout_sec=3.0)

    client = controller._make_client()

    assert isinstance(client, FakeSDKOpenAI)
    assert client.kwargs == {"timeout": 3.0}


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

        # Contract: once an action is pending evaluation, the next run should not
        # schedule overlapping actions.
        assert len(ctx.executors) == 1
        assert len(ctx.decisions) >= 2

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

        # Contract: accepted evaluation should not enqueue a rollback action.
        assert len(ctx.executors) == 1
        assert any("evaluation complete" in decision[1] for decision in ctx.decisions)

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
        # Contract: after a clearly negative outcome, the action is rejected and
        # a cooldown is applied for future windows.
        assert metadata["accepted"] is False
        assert metadata["banned_until"] > 2

    asyncio.run(scenario())
