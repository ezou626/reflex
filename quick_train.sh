#!/usr/bin/env bash
set -euo pipefail

while getopts "r" opt; do
    case $opt in
        r) rm -f /home/ubuntu/reflex/implementations/controllers/workload_classifier/models/experiments.jsonl /home/ubuntu/reflex/implementations/controllers/workload_classifier/models/library.json /home/ubuntu/reflex/implementations/controllers/workload_classifier/models/gp_*.pkl /home/ubuntu/reflex/implementations/controllers/workload_classifier/models/loader.log ;;
    esac
done

cd /home/ubuntu/reflex

# Extreme CPU
sudo .venv/bin/python3 scripts/tune_experiment2.py \
    --stressor "stress-ng --cpu $(nproc) --cpu-method matrixprod" \
    --experiments 16 --duration 30

# Extreme IO
sudo .venv/bin/python3 scripts/tune_experiment2.py \
    --stressor "fio --name=t --rw=randrw --bs=4k --size=1G --numjobs=4 --iodepth=32 --direct=1 --time_based --runtime=30 --filename=/tmp/reflex_qt.fio" \
    --experiments 16 --duration 30

# Extreme mem
sudo .venv/bin/python3 scripts/tune_experiment2.py \
    --stressor "stress-ng --vm 2 --vm-bytes 90% --vm-populate --vm-method flip" \
    --experiments 16 --duration 30

# Heavy mixed 1: CPU + mem + disk
sudo .venv/bin/python3 scripts/tune_experiment2.py \
    --stressor "stress-ng --cpu 4 --vm 2 --vm-bytes 60% --hdd 2 --io 2" \
    --experiments 16 --duration 30

# Heavy mixed 2: IO-heavy + CPU
sudo .venv/bin/python3 scripts/tune_experiment2.py \
    --stressor "fio --name=t --rw=randrw --bs=4k --size=512M --numjobs=2 --iodepth=16 --direct=1 --time_based --runtime=30 --filename=/tmp/reflex_qt2.fio" \
    --experiments 16 --duration 30

# Light workload
sudo .venv/bin/python3 scripts/tune_experiment2.py \
    --stressor "stress-ng --cpu 1 --vm 1 --vm-bytes 20% --vm-keep" \
    --experiments 16 --duration 30

sudo .venv/bin/python3 scripts/kmeans.py
