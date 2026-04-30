# Reflex Setup Instructions

## Get Started

This will help you run a benchmark. Please ensure you have root access.

### Linux/MacOS

KVM is needed

```bash
# bash
./scripts/setup_dev_env.sh
# run unixbench benchmark
./benchmarks/unixbench_qemu.sh
```

### Windows

Hardware virtualization needed

```powershell
# windows
.\scripts\setup.ps1
# run unixbench benchmark
powershell -ExecutionPolicy Bypass -File .\benchmarks\unixbench_qemu.ps1 -Modes "classifier,heuristic"
```

### Description

This project aims to follow a multi-step approach, combining work from KConfigTune and KernTune to create a pre-trained workload-optimized tuning setup for Linux,
which is then applied in real time based on clustering to fit the fingerprint of the current running workload.

Training can be launched with the `train.sh` script, the repo contains our base model in `implementations/controllers/workload_classifier/models/library.json` (which can be used directly without training). Running the training script
provides an `-r` flag to reset the model and use entirely new training workloads. Training observations accumulate in `implementations/controllers/workload_classifier/models/experiments.jsonl`.

Workloads can individually be trained with `sudo uv run python scripts/tune_experiment.py` on a given workload, or by running the train script which can do several predefined workloads in succession.

The actual online tuner is launched using `run.sh`, which supports a `-v` verbose mode to print changes in the runtime tuning knobs.

Once running run.sh, the system is now in reflex mode. To test certain workloads with reflex enabled, launch them with the `stressor.sh` script, which whitelists the corresponding cgid for
eBPF telemetry to reduce system noise. `run_stress.sh` is a sample script which launches a handful of stressors every 10 seconds, showing the system switching configs based on which stressor
has loaded.
