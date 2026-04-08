## Reflex repo quickstart

- **Clone and setup**
  - `git clone <this-repo>`
  - `cd reflex`
  - `git submodule update --init external/KernMLOps` (optional; reference for eBPF/BCC patterns, see [KernMLOps](https://github.com/ldos-project/KernMLOps))
  - `scripts/setup_dev_env.sh`

- **Key directories**
  - `ebpf/` – eBPF programs (e.g. MVP ring buffer)
  - `daemon/` – userspace daemon (`main.py` for the MVP collector)
  - `scripts/` – `setup_dev_env.sh`, `test_mvp_qemu.sh`, etc.
  - `src/reflex/` – Python package stub for uv
  - `external/KernMLOps/` – optional reference submodule

- **MVP: ring buffer → JSONL file (on the host)**
  - **Requires a real Linux environment** (bare metal, VM, or WSL2 with eBPF/BCC working) and **root** to load programs.
  - **Dependencies:** run `scripts/setup_dev_env.sh` once. It installs **`python3-bpfcc`** (and friends) from apt and runs **`uv venv --system-site-packages`** so `uv run` can import the distro **bcc** module. If `import bcc` fails, re-run setup or: `uv venv --system-site-packages --allow-existing && uv sync`.
  - Loads `ebpf/mvp_ringbuf.bpf.c` via BCC, traces `sched:sched_process_exec`, appends one JSON object per line (default file: `data/mvp_ringbuf.jsonl`).
  - Run from repo root:
    - `sudo uv run python daemon/main.py`
    - `sudo uv run python daemon/main.py -o /tmp/events.jsonl`
    - Optional: `--timeout-ms 250` (ring buffer poll interval).
  - Stop with **Ctrl+C**.
  - **Alternative without uv:** `sudo python3 daemon/main.py` (same args) if **`python3-bpfcc`** is installed on the system interpreter.

- **MVP in QEMU/KVM (Ubuntu cloud image)**
  - Needs read access to `/dev/kvm` (add your user to group `kvm` and re-open WSL, or run the script with `sudo`).
  - `cloud-image-utils` (for `cloud-localds`) is installed by `scripts/setup_dev_env.sh`; install manually only if you skipped setup: `sudo apt install cloud-image-utils`.
  - `bash scripts/test_mvp_qemu.sh` or `sudo bash scripts/test_mvp_qemu.sh`
  - First run downloads the Ubuntu 24.04 cloud image (~600 MiB) into `~/.cache/reflex-qemu` (or `REFLEX_VM_CACHE`).
  - The script expands the guest disk to **20 GiB** by default (`REFLEX_VM_DISK_GB`); the stock cloud image is too small for BCC + kernel headers + LLVM without that.
