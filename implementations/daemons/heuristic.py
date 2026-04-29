from __future__ import annotations

import argparse
import os

DAEMON_ID = "heuristic"
DESCRIPTION = "Current Reflex eBPF/window aggregator with the heuristic sysctl controller."


def create_daemon(args: argparse.Namespace):
    from daemon_core import Daemon, QueueSizes
    from daemon_core.tuners import TunerRegistry
    from implementations.aggregators import CurrentPayloadAggregator
    from implementations.controllers.heuristic import HeuristicController

    registry = TunerRegistry.from_catalog(args.tuner_catalog)
    loader_cmd: list[str] = []
    if not args.no_sudo:
        loader_cmd.append("sudo")
    loader_cmd.extend([str(args.loader_binary), str(os.getpid())])
    loader_cmd.extend(str(cgid) for cgid in args.cgroup_id)
    return Daemon(
        aggregator=CurrentPayloadAggregator(loader_cmd, window_sec=args.window_sec),
        controller=HeuristicController(registry),
        queue_sizes=QueueSizes(
            samples=args.sample_queue_size,
            controller_runs=args.controller_queue_size,
            executors=args.executor_queue_size,
        ),
        dry_run=args.dry_run,
    )
