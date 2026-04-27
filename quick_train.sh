#!/usr/bin/env bash
set -euo pipefail

while getopts "r" opt; do
    case $opt in
        r) rm -f /home/ubuntu/reflex/models/experiments.jsonl /home/ubuntu/reflex/models/library.json /home/ubuntu/reflex/models/gp_*.pkl ;;
    esac
done

sudo /home/ubuntu/reflex/.venv/bin/python3 /home/ubuntu/reflex/scripts/tune_experiment.py --stressor "stress-ng --vm 2 --vm-bytes 70% --vm-keep" --experiments 3 --duration 20 --warmup 8 --settle 5 --skip-cluster

sudo /home/ubuntu/reflex/.venv/bin/python3 /home/ubuntu/reflex/scripts/tune_experiment.py --stressor "stress-ng --cpu $(nproc) --cpu-method matrixprod" --experiments 3 --duration 20 --warmup 8 --settle 5 --skip-cluster

sudo /home/ubuntu/reflex/.venv/bin/python3 /home/ubuntu/reflex/scripts/tune_experiment.py --stressor "fio --name=t --rw=randrw --bs=4k --size=512M --numjobs=2 --iodepth=16 --direct=1 --time_based --runtime=120 --filename=/tmp/reflex_qt.fio" --experiments 3 --duration 20 --warmup 8 --settle 5 --skip-cluster

sudo /home/ubuntu/reflex/.venv/bin/python3 /home/ubuntu/reflex/scripts/tune_experiment.py --stressor "stress-ng --cpu 2 --vm 2 --vm-bytes 50% --vm-keep" --experiments 3 --duration 20 --warmup 8 --settle 5 --skip-cluster

sudo /home/ubuntu/reflex/.venv/bin/python3 /home/ubuntu/reflex/scripts/tune_experiment.py --stressor "stress-ng --vm 2 --vm-bytes 80% --vm-keep --hdd 2" --experiments 3 --duration 20 --warmup 8 --settle 5 --skip-cluster

/home/ubuntu/reflex/.venv/bin/python3 -c "
import sys
sys.path.insert(0, '/home/ubuntu/reflex/daemon')
sys.path.insert(0, '/home/ubuntu/reflex/scripts')
from tune_experiment import cluster_and_save_library
from pathlib import Path
cluster_and_save_library(Path('/home/ubuntu/reflex/models/experiments.jsonl'), Path('/home/ubuntu/reflex/models/library.json'), catalog_path=Path('/home/ubuntu/reflex/configs/tuner_catalog.yaml'), max_k=5)
"
