## Reflex repo quickstart

- **Clone and setup**
  - `git clone <this-repo>`
  - `cd reflex`
  - `git submodule update --init external/KernMLOps` (optional; reference for eBPF/BCC patterns, see [KernMLOps](https://github.com/ldos-project/KernMLOps))
  - `scripts/setup_dev_env.sh`

- **Key directories**
  - `ebpf/` – kernel telemetry (eBPF programs)
  - `daemon/` – Python userspace daemon and tuners
  - `benchmarks/` – workload scripts (CPU, IO, memory, mixed)
  - `configs/` – tuning and profile configuration
  - `scripts/` – helper scripts (setup, VMs, benchmarks, reset)
  - `src/reflex/` – minimal Python package for uv

- **MVP: ring buffer → JSONL file**
  - Loads `ebpf/mvp_ringbuf.bpf.c` (BCC), traces `sched:sched_process_exec`, appends one JSON object per line under `data/mvp_ringbuf.jsonl` by default.
  - `sudo uv run python daemon/main.py`
  - `sudo uv run python daemon/main.py -o /tmp/events.jsonl`
