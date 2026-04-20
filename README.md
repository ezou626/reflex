## Reflex repo quickstart

- **Clone and setup**
  - `git clone <this-repo>`
  - `cd reflex`
  - `git submodule update --init external/KernMLOps external/bpftune` (optional; references for eBPF/BCC patterns and tuning architecture)
  - `scripts/setup_dev_env.sh`

- **Key directories**
  - `TODO.md` – backlog for **extra host / benchmark metrics** (implemented vs planned).
  - `ebpf/` – eBPF programs (e.g. MVP ring buffer)
  - `daemon/` – userspace daemon (`main.py` for the MVP collector)
  - `scripts/` – `setup_dev_env.sh`, `test_mvp_qemu.sh`, etc.
  - `src/reflex/` – Python package stub for uv
  - `external/KernMLOps/` – optional reference submodule
  - `external/bpftune/` – optional baseline reference for tuner/event concepts ([oracle/bpftune](https://github.com/oracle/bpftune))

- **MVP: ring buffer → JSONL files (on the host)**
  - **Requires a real Linux environment** (bare metal, VM, or WSL2 with eBPF/BCC working) and **root** to load programs.
  - **Dependencies:** run `scripts/setup_dev_env.sh` once. It installs **`python3-bpfcc`** (and friends) from apt and runs **`uv venv --system-site-packages`** so `uv run` can import the distro **bcc** module. If `import bcc` fails, re-run setup or: `uv venv --system-site-packages --allow-existing && uv sync`.
  - Loads `ebpf/mvp_ringbuf.bpf.c` via BCC and collects Phase-1 metrics:
    - process churn (`fork`/`exec`/`exit`)
    - context switch rate (`sched_switch`)
    - syscall error rate (`raw_syscalls:sys_exit`)
    - wakeup->oncpu latency (`sched_wakeup` + `sched_switch`)
  - Run from repo root:
    - `sudo uv run python daemon/main.py`
    - `sudo uv run python daemon/main.py -o /tmp/events.jsonl --summary-output /tmp/summary.jsonl`
    - Optional: `--timeout-ms 200 --window-sec 1.0 --proc-sample-sec 1.0`
  - Stop with **Ctrl+C**.
  - Outputs:
    - Raw events JSONL (`data/mvp_events.jsonl` by default)
    - Window summaries JSONL (`data/mvp_summary.jsonl` by default)
  - **Alternative without uv:** `sudo python3 daemon/main.py` (same args) if **`python3-bpfcc`** is installed on the system interpreter.
  - Feature contracts and ML-oriented schema live in `configs/features_schema.yaml`.
  - Additional source catalog (for future collectors) lives in `docs/metrics_catalog.md`.

- **MVP in QEMU/KVM (Ubuntu cloud image)**
  - Needs read access to `/dev/kvm` (add your user to group `kvm` and re-open WSL, or run the script with `sudo`).
  - `cloud-image-utils` (for `cloud-localds`) is installed by `scripts/setup_dev_env.sh`; install manually only if you skipped setup: `sudo apt install cloud-image-utils`.
  - `bash scripts/test_mvp_qemu.sh` or `sudo bash scripts/test_mvp_qemu.sh`
  - First run downloads the Ubuntu 24.04 cloud image (~600 MiB) into `data/qemu` by default (or `REFLEX_VM_CACHE`).
  - The script expands the guest disk to **20 GiB** by default (`REFLEX_VM_DISK_GB`); the stock cloud image is too small for BCC + kernel headers + LLVM without that.

- **Tuning framework dev loop**
  - Daemon now supports a separate decision/action stream:
    - `--run-id`, `--run-dir` for run artifacts under `data/runs/<run-id>`
    - `--decision-log-output` for decision/action/rollback JSONL
    - `--policy-file` (YAML; see [`configs/tuning_policy.yaml`](configs/tuning_policy.yaml)) and `--tuner-catalog` (sysctl catalog; [`configs/tuner_catalog.yaml`](configs/tuner_catalog.yaml))
    - `--external-proposals` optional JSONL of actions from an external or ML controller
    - `--dry-run` for baseline collection without applying tunables
    - `uv sync --extra dev && uv run pytest` runs catalog and sysctl helper tests
  - **Benchmarks and workload scenarios**
    - **What a scenario is:** a named entry under `profiles:` in [`configs/profiles.yaml`](configs/profiles.yaml). Each profile has:
      - **`command`** — shell one-liner (usually `stress-ng …` or `fio …`) that is the main workload.
      - **`duration_sec`** — documented steady duration; for `stress-ng`, keep `--timeout` in the command aligned with this (the matrix prints a warning if they differ).
      - **`warmup_sec`** — before the main timed run, the harness runs the same `command` under `timeout` for this many seconds so the system settles; scorecards can still filter on the **main** workload window via `run_metadata.json`.
    - **Built-in scenarios (examples):** `cpu_bound`, `io_bound` (needs `fio`), `memory_pressure`, `mixed_desktop`, `sched_churn`, `memory_io_mixed`, `syscall_stress`, `wsl_safe` (~30 s, good for stable medians), `wsl_quick` (~15 s smoke).
    - **Run one mode / one scenario (bash):** from repo root, all use the same idle/warmup schedule; override idle with env vars on `run_profile.sh` only:
      - `bash benchmarks/run_profile.sh <profile> heuristic`
      - `bash benchmarks/run_profile.sh <profile> noop`
      - `bash benchmarks/run_profile.sh <profile> workload_only`
      - Optional: `IDLE_BEFORE_SEC=3 IDLE_AFTER_SEC=2 bash benchmarks/run_profile.sh wsl_safe heuristic`
      - Artifacts: `data/runs/profile-<profile>-<mode>-<timestamp>/` — `summary.jsonl`, `workload.log`, `run_metadata.json`, plus `daemon.log` / `decisions.jsonl` when not `workload_only`.
    - **Run a full comparison matrix (Python, recommended):** one command runs each requested daemon mode, then `workload_only`, then scorecards. Requires **sudo** for daemon modes (BCC).
      - `uv run python benchmarks/run_controller_matrix.py --profile wsl_safe --controller-modes heuristic,noop --include-workload-only`
      - **Multiple trials (less noise):** `--trials 10` repeats the full sequence with new run IDs; outputs include `data/runs/<prefix>-<profile>-batch-<ts>/index.json`, `scorecard_three_way_median.json`, and **`scorecard_three_way_median.summary.txt`**. Stdout shows the **compact table**; keep the JSON for min/max per trial.
      - Useful flags: `--run-prefix matrix`, `--idle-before-sec`, `--idle-after-sec`, `--warmup-sec`, `--scorecard-drop-first` / `--scorecard-drop-last`, `--scorecard-no-workload-window`, `--scorecard-include-psi-totals`, `--no-workload-only` (skip baseline sampler run).
    - **Manual scorecard on existing runs:** if you already have three `summary.jsonl` paths:
      - `uv run python benchmarks/scorecard_three_way.py [--filter-workload-window] …/heuristic/…/summary.jsonl …/noop/…/summary.jsonl …/workload_only/…/summary.jsonl`
      - `uv run python benchmarks/scorecard_compact.py path/to/scorecard_three_way.json` — writes `.summary.txt` and prints the table.
      - Re-aggregate trial JSONs: `uv run python benchmarks/scorecard_trials_aggregate.py -o med.json t0.json t1.json …` → also writes `med.summary.txt`; use `--json-stdout` if you need JSON on stdout.
    - **Adding a new scenario (profile):**
      1. Pick a unique YAML key under `profiles:` (e.g. `my_scenario`).
      2. Set **`command`** to a workload that exits on its own (timeouts in the command are ideal). Prefer tools already used in the repo (**`stress-ng`**, **`fio`**) so CI and VMs stay reproducible.
      3. Set **`duration_sec`** to match the intended steady length (e.g. same number as `stress-ng --timeout N`).
      4. Set **`warmup_sec`** (can be `0`). Warmup uses `timeout <warmup_sec> bash -lc '<command>'`; keep warmup shorter than or equal to a full run if the command ignores partial runs.
      5. Install any new binaries on the machine (`stress-ng`, `fio`, etc.).
      6. Smoke-test: `bash benchmarks/run_profile.sh my_scenario workload_only` then `bash benchmarks/run_profile.sh my_scenario noop` (needs BCC).
      7. Run the matrix: `uv run python benchmarks/run_controller_matrix.py --profile my_scenario --trials 3 --controller-modes heuristic,noop`
    - **Interpreting scorecards:** heuristic vs noop reflects **controller + sysctl side effects**, not a pure baseline A/B. Heuristic or noop vs **workload_only** shows **daemon + eBPF + harness** vs host sampling only. Prefer **`wsl_safe`** or other 30 s profiles when you care about loadavg; use **`wsl_quick`** for fast iteration. Cumulative PSI `*_total` keys are dropped from the default scorecard; see `scorecard_three_way.py` flags to include them.
  - Per-run report generation:
    - `python benchmarks/report_run.py data/runs/<run-id>`
    - `python benchmarks/report_run.py data/runs/<run-id> --format json --output data/runs/<run-id>/report.json`
  - End-to-end QEMU-backed loop:
    - `bash scripts/run_dev_loop_qemu.sh cpu_bound`
