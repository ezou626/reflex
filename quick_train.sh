#!/usr/bin/env bash
set -euo pipefail

while getopts "r" opt; do
    case $opt in
        r) rm -f /home/ubuntu/reflex/models/experiments.jsonl /home/ubuntu/reflex/models/library.json /home/ubuntu/reflex/models/gp_*.pkl /home/ubuntu/reflex/models/loader.log ;;
    esac
done

cd /home/ubuntu/reflex

sudo .venv/bin/python3 scripts/tune_experiment2.py --stressor "stress-ng --vm 2 --vm-bytes 70% --vm-keep" --experiments 3 --duration 20

sudo .venv/bin/python3 scripts/tune_experiment2.py --stressor "stress-ng --cpu $(nproc) --cpu-method matrixprod" --experiments 3 --duration 20

sudo .venv/bin/python3 scripts/tune_experiment2.py --stressor "fio --name=t --rw=randrw --bs=4k --size=512M --numjobs=2 --iodepth=16 --direct=1 --time_based --runtime=120 --filename=/tmp/reflex_qt.fio" --experiments 3 --duration 20

sudo .venv/bin/python3 scripts/tune_experiment2.py --stressor "stress-ng --cpu 2 --vm 2 --vm-bytes 50% --vm-keep" --experiments 3 --duration 20

sudo .venv/bin/python3 scripts/tune_experiment2.py --stressor "stress-ng --vm 2 --vm-bytes 80% --vm-keep --hdd 2" --experiments 3 --duration 20

sudo .venv/bin/python3 scripts/kmeans.py
