#!/usr/bin/env python3
"""Userspace MVP: poll the eBPF ring buffer and append JSON lines to a file.

Requires BCC (e.g. Ubuntu: apt install python3-bpfcc). The dev venv is configured
with system site packages so `uv run` can import the distro bcc module.

Reference implementation style: external/KernMLOps/.../fork_and_exit.py (load,
poll loop, event callbacks) — this daemon uses a ring buffer instead of perf
buffers to match the Reflex telemetry design.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def main() -> int:
    try:
        from bcc import BPF
    except ImportError:
        print(
            "error: Python module 'bcc' not found. Install BCC bindings, e.g.\n"
            "  sudo apt install python3-bpfcc bpfcc-tools\n"
            "Then recreate the venv with system site packages, e.g.\n"
            "  uv venv --system-site-packages --allow-existing && uv sync\n"
            "(scripts/setup_dev_env.sh does this automatically.)",
            file=sys.stderr,
        )
        return 1

    parser = argparse.ArgumentParser(
        description="Poll Reflex MVP eBPF ring buffer and append events to a file."
    )
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=_repo_root() / "data" / "mvp_ringbuf.jsonl",
        help="JSONL output path (default: data/mvp_ringbuf.jsonl)",
    )
    parser.add_argument(
        "--timeout-ms",
        type=int,
        default=250,
        help="ring_buffer_poll timeout in milliseconds",
    )
    args = parser.parse_args()

    bpf_c = _repo_root() / "ebpf" / "mvp_ringbuf.bpf.c"
    if not bpf_c.is_file():
        print(f"error: missing eBPF source: {bpf_c}", file=sys.stderr)
        return 1

    args.output.parent.mkdir(parents=True, exist_ok=True)

    b = BPF(src_file=str(bpf_c))

    with args.output.open("a", encoding="utf-8", buffering=1) as out:

        def on_event(_ctx, data, _size) -> None:
            ev = b["events"].event(data)
            comm = ev.comm.decode("utf-8", errors="replace").rstrip("\0")
            line = json.dumps(
                {"pid": int(ev.pid), "ts_ns": int(ev.ts_ns), "comm": comm},
                ensure_ascii=False,
            )
            out.write(line + "\n")

        b["events"].open_ring_buffer(on_event)

        print(
            f"Writing events to {args.output} (sched:sched_process_exec). "
            "Ctrl+C to stop.",
            flush=True,
        )
        try:
            while True:
                b.ring_buffer_poll(timeout=args.timeout_ms)
        except KeyboardInterrupt:
            print("\nStopped.", flush=True)
        finally:
            b.cleanup()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
