from __future__ import annotations

import argparse
import asyncio
import importlib
import json
import logging
import pkgutil
import signal
from pathlib import Path
from types import ModuleType

import reflex.implementations.daemons as daemon_configs
from reflex.core import Runtime


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _load_daemon_configs() -> dict[str, ModuleType]:
    out: dict[str, ModuleType] = {}
    prefix = daemon_configs.__name__ + "."
    for info in pkgutil.iter_modules(daemon_configs.__path__, prefix):
        mod = importlib.import_module(info.name)
        daemon_id = getattr(mod, "DAEMON_ID", None)
        create = getattr(mod, "create_daemon", None)
        if isinstance(daemon_id, str) and callable(create):
            out[daemon_id] = mod
    return dict(sorted(out.items()))


def _daemon_help(configs: dict[str, ModuleType]) -> str:
    if not configs:
        return "no daemon configs found"
    lines = ["available daemon ids:"]
    for daemon_id, mod in configs.items():
        desc = getattr(mod, "DESCRIPTION", "")
        lines.append(f"  {daemon_id}: {desc}")
    return "\n".join(lines)


def _add_common_args(parser: argparse.ArgumentParser, root: Path) -> None:
    parser.add_argument(
        "--loader-binary",
        type=Path,
        default=root / "implementations" / "ebpf" / "build" / "reflex",
        help="Path to the implementation-local loader binary.",
    )
    parser.add_argument("--window-sec", type=float, default=1.0)
    parser.add_argument("--cgroup-id", action="append", type=int, default=[])
    parser.add_argument("--sample-queue-size", type=int, default=1024)
    parser.add_argument("--controller-queue-size", type=int, default=128)
    parser.add_argument("--executor-queue-size", type=int, default=128)


async def _run(args: argparse.Namespace, config: ModuleType) -> None:
    daemon = config.create_daemon(args)
    daemon.event_retention = None if args.event_retention < 0 else args.event_retention
    daemon.execution_result_retention = (
        None
        if args.execution_result_retention < 0
        else args.execution_result_retention
    )
    event_logger: logging.Logger | None = None
    execution_logger: logging.Logger | None = None
    if args.run_dir is not None:
        run_dir = args.run_dir
        run_dir.mkdir(parents=True, exist_ok=True)
        event_logger = _configure_event_logger(run_dir, args.run_id, args.daemon_id)
        execution_logger = _configure_execution_logger(run_dir, args.run_id, args.daemon_id)
        metadata = {
            "run_id": args.run_id,
            "daemon_id": args.daemon_id,
            "run_dir": str(run_dir),
            "dry_run": args.dry_run,
            "no_sudo": args.no_sudo,
            "loader_binary": str(args.loader_binary),
            "window_sec": args.window_sec,
            "cgroup_id": args.cgroup_id,
        }
        (run_dir / "run_metadata.json").write_text(
            json.dumps(metadata, indent=2, default=str),
            encoding="utf-8",
        )
    runtime = Runtime(
        daemon,
        event_logger=event_logger,
        execution_logger=execution_logger,
    )

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, lambda: asyncio.create_task(runtime.stop()))

    await runtime.run()


def _configure_event_logger(run_dir: Path, run_id: str | None, daemon_id: str) -> logging.Logger:
    logger_name = f"reflex.runtime.{daemon_id}.{run_id or 'run'}"
    logger = logging.getLogger(logger_name)
    logger.setLevel(logging.INFO)
    logger.propagate = False

    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()

    handler = logging.FileHandler(run_dir / "daemon.log", mode="w", encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(handler)
    return logger


def _configure_execution_logger(
    run_dir: Path,
    run_id: str | None,
    daemon_id: str,
) -> logging.Logger:
    logger_name = f"reflex.execution.{daemon_id}.{run_id or 'run'}"
    logger = logging.getLogger(logger_name)
    logger.setLevel(logging.INFO)
    logger.propagate = False

    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()

    handler = logging.FileHandler(run_dir / "changes_applied.jsonl", mode="w", encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(handler)
    return logger


def main() -> int:
    root = _repo_root()
    configs = _load_daemon_configs()
    parser = argparse.ArgumentParser(
        description="Run a daemon_core Reflex daemon config.",
        epilog=_daemon_help(configs),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--no-sudo", action="store_true")
    parser.add_argument("--run-id", default=None)
    parser.add_argument(
        "--event-retention",
        type=int,
        default=4096,
        help="Maximum in-memory daemon events retained; use -1 for unlimited.",
    )
    parser.add_argument(
        "--execution-result-retention",
        type=int,
        default=1024,
        help="Maximum in-memory execution results retained; use -1 for unlimited.",
    )
    parser.add_argument(
        "--run-dir",
        type=Path,
        default=None,
        help="Directory for daemon events, changes_applied artifact, and run metadata.",
    )
    subparsers = parser.add_subparsers(
        dest="daemon_id",
        metavar="daemon_id",
        required=True,
    )

    for daemon_id, config in configs.items():
        desc = getattr(config, "DESCRIPTION", "")
        subparser = subparsers.add_parser(
            daemon_id,
            description=desc,
            help=desc,
        )
        _add_common_args(subparser, root)
        configure_parser = getattr(config, "configure_parser", None)
        if callable(configure_parser):
            configure_parser(subparser)
        subparser.set_defaults(config=config)

    args = parser.parse_args()
    asyncio.run(_run(args, args.config))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
