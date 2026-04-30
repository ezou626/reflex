#!/usr/bin/env bash
# Cycle through workloads one at a time so the classifier sees clean transitions.
# Each stressor runs alone for $HOLD seconds, then is killed before the next starts.
set -euo pipefail

HOLD="${HOLD:-30}"

run() {
    local cmd="$1"
    echo "============================================================"
    echo "  $cmd"
    echo "============================================================"
    sudo ./stressor.sh "$cmd"
    sleep "$HOLD"
    sudo pkill -f "$(echo "$cmd" | awk '{print $1}')" || true
    sleep 3
}

run "stress-ng --vm 2 --vm-bytes 70% --vm-keep"
run "stress-ng --cpu $(nproc) --cpu-method matrixprod"
run "fio --name=t --rw=randrw --bs=4k --size=4G --numjobs=2 --iodepth=16 --direct=1 --time_based --runtime=$HOLD --filename=/tmp/reflex_test.fio"
run "stress-ng --cpu 2 --vm 2 --vm-bytes 50% --vm-keep"
run "stress-ng --pipe $(nproc) --pipe-size 4k"
run "sysbench memory --memory-block-size=1M --memory-total-size=999999G run"

echo "Done."
