#!/usr/bin/env bash
# Run UnixBench under selected daemon_core implementations and write results to CSV.
# Usage: benchmarks/unixbench_compare.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="${SCRIPT_DIR%/benchmarks}"
UNIXBENCH="${UNIXBENCH:-${REPO}/external/byte-unixbench/UnixBench/Run}"
if [[ ! -x "${UNIXBENCH}" ]]; then
    UNIXBENCH="$(command -v unixbench || true)"
fi
if [[ -z "${UNIXBENCH}" || ! -x "${UNIXBENCH}" ]]; then
    cat >&2 <<EOF
error: UnixBench runner not found.
Set UNIXBENCH to the UnixBench Run script, for example:
  UNIXBENCH=/path/to/byte-unixbench/UnixBench/Run $0
EOF
    exit 1
fi
UNIXBENCH_DIR="$(cd "$(dirname "${UNIXBENCH}")" && pwd)"
UNIXBENCH_BIN="./$(basename "${UNIXBENCH}")"
BENCH_ARGS=(-i 1 dhry2reg whetstone-double)
BENCH_CMD="cd ${UNIXBENCH_DIR} && ${UNIXBENCH_BIN} ${BENCH_ARGS[*]}"
OUT="${OUT:-${REPO}/data/unixbench_results.csv}"
RUN_ROOT="${RUN_ROOT:-${REPO}/data/runs/unixbench-$(date +%Y%m%d-%H%M%S)}"
MODES_CSV="${MODES:-workload_only,heuristic,classifier}"
IFS=',' read -r -a MODES <<< "${MODES_CSV}"
UV_BIN="$(command -v uv || true)"
if [[ -z "${UV_BIN}" ]]; then
    echo "error: uv not found on PATH" >&2
    exit 1
fi

mkdir -p "$REPO/data" "$RUN_ROOT"

parse_score() {
    grep "System Benchmarks Index Score" "$1" | tail -1 | awk '{print $NF}' || true
}

run_unixbench() {
    local log=$1
    (
        cd "$UNIXBENCH_DIR"
        "$UNIXBENCH_BIN" "${BENCH_ARGS[@]}"
    ) > "$log" 2>&1
}

run_bench() {
    local mode=$1
    local log
    local run_dir="$RUN_ROOT/$mode"
    mkdir -p "$run_dir"
    log="$run_dir/workload.log"

    echo "[*] Running mode: $mode" >&2

    # Clean cgroup file
    sudo rm -f /tmp/reflex_cgroups
    sudo touch /tmp/reflex_cgroups
    sudo chmod 666 /tmp/reflex_cgroups

    if [[ "$mode" == "workload_only" ]]; then
        run_unixbench "$log"
        printf '{"mode":"%s","run_dir":"%s","bench_cmd":"%s"}\n' \
            "$mode" "$run_dir" "$BENCH_CMD" > "$run_dir/run_metadata.json"
    else
        # 1. Create cgroup and pass its id directly to the implementation loader.
        local cgdir="/sys/fs/cgroup/reflex_ubench_$$"
        sudo mkdir -p "$cgdir"
        local cgid
        cgid=$(stat -c %i "$cgdir")

        # 2. Start daemon_core implementation.
        sudo env "PATH=${PATH}" "UV_CACHE_DIR=${UV_CACHE_DIR:-/tmp/uv-cache}" \
            "${UV_BIN}" run python -m implementations.main \
            --no-sudo \
            --run-id "unixbench-$mode" \
            --run-dir "$run_dir" \
            "$mode" \
            --cgroup-id "$cgid" \
            > "$run_dir/daemon.log" 2>&1 &
        local dpid=$!
        sleep 2

        # 3. Run UnixBench inside that cgroup.
        run_unixbench "$log" &
        local bpid=$!
        echo "$bpid" | sudo tee "$cgdir/cgroup.procs" > /dev/null
        wait "$bpid"

        sudo kill "$dpid" 2>/dev/null || true
        wait "$dpid" 2>/dev/null || true
        sudo rmdir "$cgdir" 2>/dev/null || true
    fi

    local score
    score=$(parse_score "$log")
    if [[ -z "$score" ]]; then
        echo "error: could not parse UnixBench score for mode=$mode" >&2
        echo "  log: $log" >&2
        echo "--- workload log tail ---" >&2
        tail -80 "$log" >&2 || true
        echo "--- daemon log tail ---" >&2
        tail -80 "$run_dir/daemon.log" >&2 || true
        return 1
    fi
    echo "    score: $score" >&2
    echo "$score"
}

echo "mode,score" > "$OUT"
echo "mode,score" > "$RUN_ROOT/unixbench_results.csv"

for mode in "${MODES[@]}"; do
    score=$(run_bench "$mode")
    echo "$mode,$score" >> "$OUT"
    echo "$mode,$score" >> "$RUN_ROOT/unixbench_results.csv"
done

echo ""
echo "Results written to $OUT"
echo "Run artifacts written to $RUN_ROOT"
echo ""
cat "$OUT"
