from __future__ import annotations

import argparse
import asyncio
import importlib
import pkgutil
import signal
from pathlib import Path
from types import ModuleType

from daemon_core import Runtime
import implementations.daemons as daemon_configs


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


async def _run(args: argparse.Namespace, config: ModuleType) -> None:
    daemon = config.create_daemon(args)
    runtime = Runtime(daemon)

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, lambda: asyncio.create_task(runtime.stop()))

    await runtime.run()


def main() -> int:
    root = _repo_root()
    configs = _load_daemon_configs()
    parser = argparse.ArgumentParser(
        description="Run a daemon_core Reflex daemon config.",
        epilog=_daemon_help(configs),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "daemon_id",
        nargs="?",
        default=next(iter(configs), ""),
        choices=tuple(configs) if configs else None,
        help="Daemon config id to run.",
    )
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
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--no-sudo", action="store_true")
    parser.add_argument("--cgroup-id", action="append", type=int, default=[])
    parser.add_argument("--sample-queue-size", type=int, default=1024)
    parser.add_argument("--controller-queue-size", type=int, default=128)
    parser.add_argument("--executor-queue-size", type=int, default=128)
    args = parser.parse_args()
    if args.daemon_id not in configs:
        parser.error("no daemon configs found")
    asyncio.run(_run(args, configs[args.daemon_id]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
