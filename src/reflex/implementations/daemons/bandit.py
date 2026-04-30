from __future__ import annotations

import argparse
import os
from pathlib import Path

DAEMON_ID = "bandit"
DESCRIPTION = "Current Reflex eBPF/window aggregator with contextual bandit sysctl tuning."


def configure_parser(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--bandit-alpha", type=float, default=0.05)
    parser.add_argument("--bandit-epsilon", type=float, default=0.10)
    parser.add_argument("--bandit-evaluate-after-windows", type=int, default=3)
    parser.add_argument("--bandit-state-path", type=Path, default=None)
    parser.add_argument("--controller-max-steps-per-run", type=int, default=1)


def create_daemon(args: argparse.Namespace):
    from reflex.core import Daemon, QueueSizes
    from reflex.core.tuners import TunerRegistry
    from reflex.implementations.aggregators import WindowSummaryAggregator
    from reflex.implementations.controllers.bandit import ContextualBanditController

    registry = TunerRegistry.default()
    loader_cmd: list[str] = []
    if not args.no_sudo:
        loader_cmd.append("sudo")
    loader_cmd.extend([str(args.loader_binary), str(os.getpid())])
    loader_cmd.extend(str(cgid) for cgid in args.cgroup_id)
    return Daemon(
        aggregator=WindowSummaryAggregator(loader_cmd, window_sec=args.window_sec),
        controller=ContextualBanditController(
            registry,
            alpha=args.bandit_alpha,
            epsilon=args.bandit_epsilon,
            evaluate_after_windows=args.bandit_evaluate_after_windows,
            state_path=args.bandit_state_path,
            max_steps_per_run=args.controller_max_steps_per_run,
        ),
        queue_sizes=QueueSizes(
            samples=args.sample_queue_size,
            controller_runs=args.controller_queue_size,
            executors=args.executor_queue_size,
        ),
        dry_run=args.dry_run,
    )
