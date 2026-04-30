from __future__ import annotations

import argparse
import asyncio
import importlib
import json
import pkgutil
import signal
from dataclasses import asdict, is_dataclass
from pathlib import Path
from types import ModuleType
from typing import Any

from reflex.core import Runtime
import reflex.implementations.daemons as daemon_configs


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
    parser.add_argument(
        "--tuner-catalog",
        type=Path,
        default=root / "configs" / "tuner_catalog.yaml",
        help="Path to the tuner catalog.",
    )
    parser.add_argument("--window-sec", type=float, default=1.0)
    parser.add_argument("--cgroup-id", action="append", type=int, default=[])
    parser.add_argument("--sample-queue-size", type=int, default=1024)
    parser.add_argument("--controller-queue-size", type=int, default=128)
    parser.add_argument("--executor-queue-size", type=int, default=128)


async def _run(args: argparse.Namespace, config: ModuleType) -> None:
    daemon = config.create_daemon(args)
    if args.run_dir is not None:
        run_dir = args.run_dir
        run_dir.mkdir(parents=True, exist_ok=True)
        metadata = {
            "run_id": args.run_id,
            "daemon_id": args.daemon_id,
            "run_dir": str(run_dir),
            "dry_run": args.dry_run,
            "no_sudo": args.no_sudo,
            "loader_binary": str(args.loader_binary),
            "tuner_catalog": str(args.tuner_catalog),
            "window_sec": args.window_sec,
            "cgroup_id": args.cgroup_id,
        }
        (run_dir / "run_metadata.json").write_text(
            json.dumps(metadata, indent=2, default=str),
            encoding="utf-8",
        )
        original_on_stop = daemon.on_stop

        async def write_artifacts(runtime: Runtime) -> None:
            if original_on_stop is not None:
                await original_on_stop(runtime)
            _write_jsonl(run_dir / "events.jsonl", runtime.events)
            _write_jsonl(run_dir / "execution_results.jsonl", runtime.execution_results)

        daemon.on_stop = write_artifacts
    runtime = Runtime(daemon)

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, lambda: asyncio.create_task(runtime.stop()))

    await runtime.run()


def _jsonable(value: Any) -> Any:
    if is_dataclass(value):
        return asdict(value)
    if isinstance(value, Path):
        return str(value)
    return str(value)


def _write_jsonl(path: Path, records: list[Any]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, indent=2, default=_jsonable) + "\n")


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
        "--run-dir",
        type=Path,
        default=None,
        help="Directory for daemon events, execution results, and run metadata.",
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
