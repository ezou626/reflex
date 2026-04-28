#!/usr/bin/env bash
# Run UnixBench under 4 conditions and write results to CSV.
# Usage: sudo ./benchmarks/unixbench_compare.sh
set -euo pipefail

REPO=/home/ubuntu/reflex
UNIXBENCH=/home/ubuntu/byte-unixbench/UnixBench/Run
BENCH_CMD="$UNIXBENCH -i 1 dhry2reg whetstone-double"
PYTHON="$REPO/.venv/bin/python3"
OUT="$REPO/data/unixbench_results.csv"
MODES=("workload_only" "noop" "heuristic" "classifier")

mkdir -p "$REPO/data"

parse_score() {
    grep "System Benchmarks Index Score" "$1" | tail -1 | awk '{print $NF}'
}

run_bench() {
    local mode=$1
    local log
    log=$(mktemp /tmp/unixbench_XXXXXX.log)

    echo "[*] Running mode: $mode"

    # Clean cgroup file
    sudo rm -f /tmp/reflex_cgroups
    sudo touch /tmp/reflex_cgroups
    sudo chmod 666 /tmp/reflex_cgroups

    if [[ "$mode" == "workload_only" ]]; then
        bash -c "$BENCH_CMD" > "$log" 2>&1
    else
        # 1. Start daemon
        sudo "$PYTHON" "$REPO/daemon/main.py" \
            --controller-mode "$mode" \
            --run-id "ubench-$mode" \
            --run-dir "/tmp/ubench-$mode" \
            > /tmp/ubench-daemon-$mode.log 2>&1 &
        local dpid=$!
        sleep 2  # let loader attach and start polling

        # 2. Create cgroup, write cgid so loader picks it up
        local cgdir="/sys/fs/cgroup/reflex_ubench_$$"
        sudo mkdir -p "$cgdir"
        local cgid
        cgid=$(stat -c %i "$cgdir")
        echo "$cgid" >> /tmp/reflex_cgroups
        sleep 0.2  # one poll cycle for loader to whitelist it

        # 3. Run UnixBench inside that cgroup
        bash -c "$BENCH_CMD" > "$log" 2>&1 &
        local bpid=$!
        echo "$bpid" | sudo tee "$cgdir/cgroup.procs" > /dev/null
        wait "$bpid"

        sudo kill "$dpid" 2>/dev/null || true
        wait "$dpid" 2>/dev/null || true
        sudo rmdir "$cgdir" 2>/dev/null || true
    fi

    local score
    score=$(parse_score "$log")
    echo "    score: $score"
    rm -f "$log"
    echo "$score"
}

echo "mode,score" > "$OUT"

for mode in "${MODES[@]}"; do
    score=$(run_bench "$mode")
    echo "$mode,$score" >> "$OUT"
done

echo ""
echo "Results written to $OUT"
echo ""
cat "$OUT"
