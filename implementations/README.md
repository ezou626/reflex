# Implementations

`implementations/` contains runnable programs built on `daemon_core`. Runtime
assets live beside the code that uses them so experiments do not contaminate the
legacy daemon.

## Layout

- `aggregators/` - aggregator packages that own loader setup, IPC, parsing, and sample emission.
- `controllers/` - controller packages; each controller gets its own subdirectory for code and assets.
- `executors/` - executor packages; each executor gets its own subdirectory.
- `triggers/` - optional trigger packages; each trigger gets its own subdirectory.
- `ebpf/` - local eBPF and loader sources plus `Makefile`.
- `daemons/` - daemon config modules. Each config exports `DAEMON_ID`, `DESCRIPTION`, and `create_daemon(args)`.
- `main.py` - discovers daemon configs in `daemons/`, lists them in `-h`, and runs the selected daemon id.

## Reflex Implementation

Build the local loader/eBPF pair:

```bash
make -C implementations/ebpf
```

Run a daemon config:

```bash
uv run python -m implementations.main heuristic --dry-run
uv run python -m implementations.main classifier --dry-run
```

Use `-h` to see all discovered daemon ids.
