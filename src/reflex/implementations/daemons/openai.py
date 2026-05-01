from __future__ import annotations

import argparse
import os

DAEMON_ID = "openai"
DESCRIPTION = "Current Reflex eBPF/window aggregator with OpenAI semantic tuning proposals."


def configure_parser(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--openai-model", default="gpt-5-mini")
    parser.add_argument("--openai-max-actions", type=int, default=1)
    parser.add_argument("--openai-history-windows", type=int, default=5)
    parser.add_argument("--openai-timeout-sec", type=float, default=15.0)
    parser.add_argument("--openai-trigger-interval-sec", type=float, default=15.0)
    parser.add_argument("--controller-max-steps-per-run", type=int, default=1)


def create_daemon(args: argparse.Namespace):
    from reflex.core import Daemon, QueueSizes, interval_trigger
    from reflex.core.tuners import TunerRegistry
    from reflex.implementations.aggregators import WindowSummaryAggregator
    from reflex.implementations.controllers.openai import OpenAITuningController

    registry = TunerRegistry.default()
    loader_cmd: list[str] = []
    if not args.no_sudo:
        loader_cmd.append("sudo")
    loader_cmd.extend([str(args.loader_binary), str(os.getpid())])
    loader_cmd.extend(str(cgid) for cgid in args.cgroup_id)
    return Daemon(
        aggregator=WindowSummaryAggregator(
            loader_cmd,
            window_sec=args.window_sec,
            trigger_on_sample=False,
        ),
        controller=OpenAITuningController(
            registry,
            model=args.openai_model,
            max_actions=args.openai_max_actions,
            history_windows=args.openai_history_windows,
            timeout_sec=args.openai_timeout_sec,
            allow_apply=not args.dry_run,
            max_steps_per_run=args.controller_max_steps_per_run,
        ),
        triggers=[interval_trigger(args.openai_trigger_interval_sec, "openai_timer")],
        queue_sizes=QueueSizes(
            samples=args.sample_queue_size,
            controller_runs=args.controller_queue_size,
            executors=args.executor_queue_size,
        ),
        dry_run=args.dry_run,
    )
