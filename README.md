## Reflex repo quickstart

- **Clone and setup**
  - `git clone <this-repo>`
  - `cd reflex`
  - `scripts/setup_dev_env.sh`

- **Key directories**
  - `ebpf/` – kernel telemetry (eBPF programs)
  - `daemon/` – Python userspace daemon and tuners
  - `benchmarks/` – workload scripts (CPU, IO, memory, mixed)
  - `configs/` – tuning and profile configuration
  - `scripts/` – helper scripts (setup, VMs, benchmarks, reset)
  - `src/reflex/` – minimal Python package for uv

- **Run the daemon (once implemented)**
  - `sudo uv run python daemon/main.py`
