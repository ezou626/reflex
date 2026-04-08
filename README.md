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

- **MVP in QEMU/KVM (Ubuntu cloud image)**
  - Needs read access to `/dev/kvm` (add your user to group `kvm` and re-open WSL, or run the script with `sudo`).
  - `cloud-image-utils` (for `cloud-localds`) is installed by `scripts/setup_dev_env.sh`; install manually only if you skipped setup: `sudo apt install cloud-image-utils`.
  - `bash scripts/test_mvp_qemu.sh` or `sudo bash scripts/test_mvp_qemu.sh`
  - First run downloads the Ubuntu 24.04 cloud image (~600 MiB) into `~/.cache/reflex-qemu` (or `REFLEX_VM_CACHE`).
  - The script expands the guest disk to **20 GiB** by default (`REFLEX_VM_DISK_GB`); the stock cloud image is too small for BCC + kernel headers + LLVM without that.
