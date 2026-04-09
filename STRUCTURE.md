# Reflex  
**Machine-Assisted Linux Performance Tuning Framework**

## Overview

Reflex is a flexible, machine-assisted framework for performance tuning on Linux systems. It combines:

- eBPF-based telemetry
- A userspace decision daemon (Python)
- Modular tuners for system parameters
- Logging, rollback, and benchmarking infrastructure
- Optional lightweight ML-driven decision making

The goal is to dynamically observe system behavior, detect bottlenecks, and apply safe, reversible tuning actions tailored to application profiles such as gaming, productivity, content creation, and studying.

This project is designed as a prototype system focused on desktop-class environments running Ubuntu 24.04 LTS.

---

## Project Scope (MVP)

The MVP focuses on:

- Real-time system telemetry using eBPF
- Feature aggregation in userspace
- Deterministic bottleneck detection
- Modular tuner framework
- Safe sysctl modification with rollback
- Reproducible benchmark framework
- Optional lightweight ML classifier for workload classification

Stretch goals (time permitting):

- Online learning
- RL-driven tuning
- Predictive tuning
- Dynamic tuner ingestion

---

## System Architecture
eBPF Programs
↓
BPF Ring Buffer
↓
Python Userspace Daemon
↓
Feature Engineering
↓
Decision Engine
↓
Tuner Plugins
↓
System Tunables (sysctl, governors, etc.)
↓
Structured Logging + Rollback

## Key design principles:

- Modularity
- Safety-first tuning
- Reproducibility
- Minimal system overhead
- Auditability via structured logs

---

## Target Environment

### Operating System
- Ubuntu 24.04 LTS
- Stock kernel (no custom kernel builds)

### Virtualization
Primary testing environment:
- QEMU/KVM virtual machines

Recommended VM profiles:
- 2 cores / 4GB RAM (low-end laptop)
- 8 cores / 16GB RAM (high-performance desktop)
- 2 cores / 2GB RAM (memory-constrained)

We use VMs to:
- Ensure reproducibility
- Isolate risky tuning
- Snapshot and rollback safely
- Simulate diverse hardware conditions

---

## Technology Stack

### Kernel-Space
- C (eBPF programs)
- clang/LLVM
- libbpf
- BPF_MAP_TYPE_RINGBUF for streaming telemetry

### User-Space
- Python (primary daemon)
- PyTorch (optional ML component via `ml` extra)
- Structured JSON logging

### Benchmarking Tools
- stress-ng (CPU & memory stress)
- fio (I/O workloads)
- hackbench (scheduler stress)
- perf (baseline comparison)

---

## Repository Structure
```
reflex/
│
├── ebpf/
│   ├── sched.bpf.c
│   ├── mm.bpf.c
│   ├── syscall.bpf.c
│   └── Makefile
│
├── daemon/
│   ├── main.py
│   ├── feature_engineering.py
│   ├── decision_engine.py
│   ├── logger.py
│   ├── rollback.py
│   └── tuners/
│       ├── base.py
│       ├── cpu_tuner.py
│       ├── memory_tuner.py
│       └── io_tuner.py
│
├── benchmarks/
│   ├── cpu_profile.sh
│   ├── io_profile.sh
│   ├── memory_profile.sh
│   └── mixed_profile.sh
│
├── experiments/
│   ├── collected_data/
│   ├── models/
│   └── notebooks/
│
├── configs/
│   ├── profiles.yaml
│   └── tunables.yaml
│
├── scripts/
│   ├── setup_dev_env.sh
│   ├── run_vm.sh
│   ├── run_benchmark.sh
│   └── reset_sysctl.sh
│
└── README.md
```

## Safety & Rollback Guarantees

Reflex enforces safety-first tuning:

- Snapshot of original sysctl values at daemon start
- Bounded tuning ranges
- Time-window performance comparison
- Automatic rollback on regression
- Dry-run mode (`--dry-run`)
- Safe mode (`--safe-mode`)

All tuning decisions are logged in structured JSON format for auditability.

---

## Logging Format

All decisions and telemetry windows are logged as structured JSON:

Something like this is what we want:
```json
{
  "timestamp": 1710000000,
  "features": { ... },
  "classification": "CPU_BOUND",
  "action": {
      "vm.swappiness": 10
  },
  "pre_metrics": { ... },
  "post_metrics": { ... },
  "rollback": false
}
```

This enables:
- Offline model training
- Performance regression analysis
- Benchmark comparison
- Demo visualization

## Development Workflow

### Setup
```bash
scripts/setup_dev_env.sh
```
Installs:
- uv (Python package manager, via installer if missing)
- build-essential
- clang
- libbpf-dev
- linux headers for the running kernel (or `linux-headers-generic` under WSL2)
- linux-tools-common (perf/bpftool tooling, or `linux-tools-generic` under WSL2)
- linux-tools-$(uname -r) (kernel-specific perf/bpftool on stock Ubuntu kernels)
- stress-ng
- fio
- qemu-system-x86 (QEMU/KVM for running test VMs)
- cloud-image-utils (`cloud-localds` for QEMU cloud-image tests, e.g. `scripts/test_mvp_qemu.sh`)

Python dependencies are managed with **uv** using `pyproject.toml` and `uv.lock`. The setup script runs `uv sync` to create/update the project environment from the lockfile.

### Running the Daemon
```bash
sudo uv run python daemon/main.py
```

Current Phase-1 telemetry:
- process churn (`fork`/`exec`/`exit`)
- context switch rate (`sched_switch`)
- syscall error rate (`raw_syscalls:sys_exit`)
- wakeup->oncpu latency (`sched_wakeup` + `sched_switch`)
- host-side low-overhead features from `/proc` and `/proc/pressure/*`

Useful flags:
`-o /tmp/events.jsonl`
`--summary-output /tmp/summary.jsonl`
`--timeout-ms 200`
`--window-sec 1.0`
`--proc-sample-sec 1.0`

### Benchmarking

Benchmarks simulate controlled workloads for evaluation:
- CPU-bound workloads
- I/O-bound workloads
- Memory pressure workloads
- Mixed workloads

Each benchmark:
- Runs a synthetic workload
- Captures telemetry
- Saves logs to structured JSON
- Enables comparison across tuning strategies

## Milestone Roadmap
1. eBPF telemetry pipeline (end-to-end)
2. Feature aggregation and windowing
3. Deterministic bottleneck detection
4. Modular tuner implementation
5. Safe rollback mechanism
6. Benchmark automation
7. Lightweight ML classifier (optional)
8. Comparative evaluation
9. Design Philosophy

## Reflex prioritizes:
- Practicality over novelty
- Stability over aggressiveness
- Explainability over black-box optimization
- Modular extensibility
- Low system overhead

The ML component is intentionally lightweight in the MVP. The system is designed such that heuristic and ML-based decision engines can be swapped interchangeably.

## Out of Scope
- Kernel source modification
- Distributed environments
- Firmware tuning
- Real-time guarantees
- Cross-machine coordination

## Long-Term Vision
Reflex aims to evolve toward:
- Online adaptive tuning
- Safe reinforcement learning
- Dynamic tuner ingestion
- Predictive system state modeling

The current prototype lays the foundation for those extensions.
