from __future__ import annotations

import argparse
import os
from pathlib import Path

DAEMON_ID = "classifier"
DESCRIPTION = "Current Reflex eBPF/window aggregator with the workload classifier controller."


def configure_parser(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--classifier-library",
        type=Path,
        default=None,
        help="Path to workload classifier library JSON.",
    )
    parser.add_argument("--classifier-max-distance", type=float, default=0.35)
    parser.add_argument("--classifier-min-consecutive", type=int, default=3)


def create_daemon(args: argparse.Namespace):
    from reflex.core import Daemon, QueueSizes
    from reflex.core.tuners import TunerRegistry
    from reflex.implementations.aggregators import WindowSummaryAggregator
    from reflex.implementations.controllers.workload_classifier import (
        DEFAULT_LIBRARY_PATH,
        WorkloadClassifier,
        WorkloadClassifierController,
    )

    registry = TunerRegistry.from_catalog(args.tuner_catalog)
    library_path = args.classifier_library or DEFAULT_LIBRARY_PATH
    classifier = WorkloadClassifier(library_path, max_distance=args.classifier_max_distance)
    loader_cmd: list[str] = []
    if not args.no_sudo:
        loader_cmd.append("sudo")
    loader_cmd.extend([str(args.loader_binary), str(os.getpid())])
    loader_cmd.extend(str(cgid) for cgid in args.cgroup_id)
    return Daemon(
        aggregator=WindowSummaryAggregator(loader_cmd, window_sec=args.window_sec),
        controller=WorkloadClassifierController(
            registry,
            classifier,
            min_consecutive=args.classifier_min_consecutive,
        ),
        queue_sizes=QueueSizes(
            samples=args.sample_queue_size,
            controller_runs=args.controller_queue_size,
            executors=args.executor_queue_size,
        ),
        dry_run=args.dry_run,
    )
