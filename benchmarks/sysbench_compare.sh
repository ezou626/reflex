#!/usr/bin/env bash
# Run sysbench under selected daemon implementations and write results to CSV.
# Usage: benchmarks/sysbench_compare.sh --modes workload_only,heuristic --test cpu
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="${SCRIPT_DIR%/benchmarks}"

SYSBENCH="${SYSBENCH:-$(command -v sysbench || true)}"
if [[ -z "${SYSBENCH}" || ! -x "${SYSBENCH}" ]]; then
    echo "error: sysbench not found on PATH. Install it or set SYSBENCH=/path/to/sysbench." >&2
    exit 1
fi

DRY_RUN=0
MODES_CSV="${MODES:-}"
TEST="${SYSBENCH_TEST:-cpu}"
TIME_SEC="${SYSBENCH_TIME:-30}"
THREADS="${SYSBENCH_THREADS:-$(getconf _NPROCESSORS_ONLN 2>/dev/null || echo 1)}"
CPU_MAX_PRIME="${SYSBENCH_CPU_MAX_PRIME:-20000}"
MEMORY_BLOCK_SIZE="${SYSBENCH_MEMORY_BLOCK_SIZE:-1M}"
MEMORY_TOTAL_SIZE="${SYSBENCH_MEMORY_TOTAL_SIZE:-20G}"
FILE_TOTAL_SIZE="${SYSBENCH_FILE_TOTAL_SIZE:-2G}"
FILE_TEST_MODE="${SYSBENCH_FILE_TEST_MODE:-rndrw}"
EXTRA_ARGS=()

usage() {
    cat >&2 <<EOF
Usage: benchmarks/sysbench_compare.sh --modes workload_only,heuristic [options]
       benchmarks/sysbench_compare.sh --modes workload_only -- --report-interval=1

Options:
  --modes CSV              Required controller modes.
  --test NAME              sysbench test: cpu, memory, threads, mutex, fileio.
  --time SEC               Test duration, default: ${TIME_SEC}.
  --threads N              sysbench threads, default: online CPU count.
  --dry-run                Also run daemon modes with _dry suffix.
  --cpu-max-prime N        cpu test max prime, default: ${CPU_MAX_PRIME}.
  --memory-block-size SIZE memory test block size, default: ${MEMORY_BLOCK_SIZE}.
  --memory-total-size SIZE memory test total size, default: ${MEMORY_TOTAL_SIZE}.
  --file-total-size SIZE   fileio prepare size, default: ${FILE_TOTAL_SIZE}.
  --file-test-mode MODE    fileio mode, default: ${FILE_TEST_MODE}.
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --modes) MODES_CSV="$2"; shift ;;
        --modes=*) MODES_CSV="${1#--modes=}" ;;
        --test) TEST="$2"; shift ;;
        --test=*) TEST="${1#--test=}" ;;
        --time) TIME_SEC="$2"; shift ;;
        --time=*) TIME_SEC="${1#--time=}" ;;
        --threads) THREADS="$2"; shift ;;
        --threads=*) THREADS="${1#--threads=}" ;;
        --dry-run) DRY_RUN=1 ;;
        --cpu-max-prime) CPU_MAX_PRIME="$2"; shift ;;
        --cpu-max-prime=*) CPU_MAX_PRIME="${1#--cpu-max-prime=}" ;;
        --memory-block-size) MEMORY_BLOCK_SIZE="$2"; shift ;;
        --memory-block-size=*) MEMORY_BLOCK_SIZE="${1#--memory-block-size=}" ;;
        --memory-total-size) MEMORY_TOTAL_SIZE="$2"; shift ;;
        --memory-total-size=*) MEMORY_TOTAL_SIZE="${1#--memory-total-size=}" ;;
        --file-total-size) FILE_TOTAL_SIZE="$2"; shift ;;
        --file-total-size=*) FILE_TOTAL_SIZE="${1#--file-total-size=}" ;;
        --file-test-mode) FILE_TEST_MODE="$2"; shift ;;
        --file-test-mode=*) FILE_TEST_MODE="${1#--file-test-mode=}" ;;
        --help|-h) usage; exit 0 ;;
        --) shift; EXTRA_ARGS+=("$@"); break ;;
        *) echo "unknown option: $1" >&2; usage; exit 1 ;;
    esac
    shift
done

if [[ -z "${MODES_CSV}" ]]; then
    echo "error: --modes is required (e.g. --modes workload_only,heuristic,classifier)" >&2
    exit 1
fi

case "${TEST}" in
    cpu|memory|threads|mutex|fileio) ;;
    *) echo "error: unsupported --test '${TEST}'" >&2; exit 1 ;;
esac

RUN_ROOT="${RUN_ROOT:-${REPO}/data/runs/sysbench-${TEST}-$(date +%Y%m%d-%H%M%S)}"
IFS=',' read -r -a MODES <<< "${MODES_CSV}"

if [[ "${DRY_RUN}" == "1" ]]; then
    EXPANDED=()
    for mode in "${MODES[@]}"; do
        EXPANDED+=("${mode}")
        [[ "${mode}" != "workload_only" ]] && EXPANDED+=("${mode}_dry")
    done
    MODES=("${EXPANDED[@]}")
fi

UV_BIN="$(command -v uv || true)"
if [[ -z "${UV_BIN}" ]]; then
    echo "error: uv not found on PATH" >&2
    exit 1
fi

mkdir -p "${REPO}/data" "${RUN_ROOT}"

make_bench_args() {
    BENCH_ARGS=()
    case "${TEST}" in
        cpu)
            BENCH_ARGS=(
                cpu
                "--threads=${THREADS}"
                "--time=${TIME_SEC}"
                "--cpu-max-prime=${CPU_MAX_PRIME}"
            )
            ;;
        memory)
            BENCH_ARGS=(memory "--threads=${THREADS}" "--time=${TIME_SEC}" \
                "--memory-block-size=${MEMORY_BLOCK_SIZE}"
                "--memory-total-size=${MEMORY_TOTAL_SIZE}"
            )
            ;;
        threads)
            BENCH_ARGS=(threads "--threads=${THREADS}" "--time=${TIME_SEC}")
            ;;
        mutex)
            BENCH_ARGS=(mutex "--threads=${THREADS}" "--time=${TIME_SEC}")
            ;;
        fileio)
            BENCH_ARGS=(fileio "--threads=${THREADS}" "--time=${TIME_SEC}" \
                "--file-total-size=${FILE_TOTAL_SIZE}" "--file-test-mode=${FILE_TEST_MODE}"
            )
            ;;
    esac
    BENCH_ARGS+=("${EXTRA_ARGS[@]}" run)
}

parse_score() {
    python3 "${REPO}/benchmarks/parse_benchmark_scores.py" < "$1" |
        python3 -c '
import json
import sys

scores = json.load(sys.stdin)["scores"]
value = scores.get("primary_value")
print("" if value is None else value)
'
}

run_sysbench() {
    local log=$1
    local work_dir=$2
    mkdir -p "${work_dir}"
    make_bench_args

    (
        cd "${work_dir}"
        echo "+ ${SYSBENCH} ${BENCH_ARGS[*]}"
        if [[ "${TEST}" == "fileio" ]]; then
            "${SYSBENCH}" fileio "--file-total-size=${FILE_TOTAL_SIZE}" prepare
        fi
        "${SYSBENCH}" "${BENCH_ARGS[@]}"
        if [[ "${TEST}" == "fileio" ]]; then
            "${SYSBENCH}" fileio "--file-total-size=${FILE_TOTAL_SIZE}" cleanup
        fi
    ) > "${log}" 2>&1
}

run_bench() {
    local mode=$1
    local run_dir="${RUN_ROOT}/${mode}"
    local log="${run_dir}/workload.log"
    local work_dir="${run_dir}/sysbench-work"
    mkdir -p "${run_dir}"

    local impl_mode="${mode}"
    local dry_flag=()
    if [[ "${mode}" == *_dry ]]; then
        impl_mode="${mode%_dry}"
        dry_flag=(--dry-run)
    fi

    echo "[*] Running sysbench ${TEST} mode: ${mode}" >&2

    sudo rm -f /tmp/reflex_cgroups
    sudo touch /tmp/reflex_cgroups
    sudo chmod 666 /tmp/reflex_cgroups

    if [[ "${impl_mode}" == "workload_only" ]]; then
        run_sysbench "${log}" "${work_dir}"
        printf '{"mode":"%s","run_dir":"%s","benchmark":"sysbench","test":"%s"}\n' \
            "${mode}" "${run_dir}" "${TEST}" > "${run_dir}/run_metadata.json"
    else
        local cgdir="/sys/fs/cgroup/reflex_sysbench_$$"
        sudo mkdir -p "${cgdir}"
        local cgid
        cgid=$(stat -c %i "${cgdir}")

        local openai_env=()
        if [[ -n "${OPENAI_API_KEY:-}" ]]; then
            openai_env=("OPENAI_API_KEY=${OPENAI_API_KEY}")
        fi

        sudo env "PATH=${PATH}" "UV_CACHE_DIR=${UV_CACHE_DIR:-/tmp/uv-cache}" \
            "${openai_env[@]}" \
            "${UV_BIN}" run reflex \
            --no-sudo \
            --run-id "sysbench-${TEST}-${mode}" \
            --run-dir "${run_dir}" \
            "${dry_flag[@]}" \
            "${impl_mode}" \
            --cgroup-id "${cgid}" \
            > "${run_dir}/daemon.log" 2>&1 &
        local dpid=$!
        sleep 2

        run_sysbench "${log}" "${work_dir}" &
        local bpid=$!
        echo "${bpid}" | sudo tee "${cgdir}/cgroup.procs" >/dev/null
        wait "${bpid}"

        sudo kill "${dpid}" 2>/dev/null || true
        wait "${dpid}" 2>/dev/null || true
        sudo rmdir "${cgdir}" 2>/dev/null || true
    fi

    local score
    score=$(parse_score "${log}")
    if [[ -z "${score}" ]]; then
        echo "error: could not parse sysbench primary score for mode=${mode}" >&2
        echo "  log: ${log}" >&2
        echo "--- workload log tail ---" >&2
        tail -80 "${log}" >&2 || true
        echo "--- daemon log tail ---" >&2
        tail -80 "${run_dir}/daemon.log" >&2 || true
        return 1
    fi
    echo "    primary score: ${score}" >&2
    echo "${score}"
}

echo "mode,primary_value" > "${RUN_ROOT}/sysbench_results.csv"

for mode in "${MODES[@]}"; do
    score=$(run_bench "${mode}")
    echo "${mode},${score}" >> "${RUN_ROOT}/sysbench_results.csv"
done

echo ""
echo "Run artifacts written to ${RUN_ROOT}"
echo ""
cat "${RUN_ROOT}/sysbench_results.csv"
