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

FAST=1
TARGETED=0
DRY_RUN=0
DRY_RUN_ONLY=0
MODES_CSV="${MODES:-}"
TRIALS="${TRIALS:-3}"
WARMUP="${WARMUP:-1}"
RANDOMIZE_ORDER="${RANDOMIZE_ORDER:-1}"

while [[ $# -gt 0 ]]; do
    case "$1" in
        --fast)       FAST=1; TARGETED=0 ;;
        --full)       FAST=0; TARGETED=0 ;;
        --targeted)   TARGETED=1 ;;
        --dry-run)      DRY_RUN=1 ;;
        --dry-run-only) DRY_RUN_ONLY=1 ;;
        --modes)      MODES_CSV="$2"; shift ;;
        --modes=*)    MODES_CSV="${1#--modes=}" ;;
        --trials)     TRIALS="$2"; shift ;;
        --trials=*)   TRIALS="${1#--trials=}" ;;
        --warmup)     WARMUP="$2"; shift ;;
        --warmup=*)   WARMUP="${1#--warmup=}" ;;
        --randomize-order) RANDOMIZE_ORDER=1 ;;
        --no-randomize-order) RANDOMIZE_ORDER=0 ;;
        *) echo "unknown option: $1" >&2; exit 1 ;;
    esac
    shift
done

if [[ -z "$MODES_CSV" ]]; then
    echo "error: --modes is required (e.g. --modes workload_only,heuristic,classifier)" >&2
    exit 1
fi
if ! [[ "$TRIALS" =~ ^[1-9][0-9]*$ ]]; then
    echo "error: --trials must be a positive integer" >&2
    exit 1
fi
if ! [[ "$WARMUP" =~ ^[0-9]+$ ]]; then
    echo "error: --warmup must be a non-negative integer" >&2
    exit 1
fi

if [[ "$TARGETED" == "1" ]]; then
    BENCH_ARGS=(-i 3 pipe context1 syscall spawn fsbuffer)
    SUITE="targeted"
elif [[ "$FAST" == "1" ]]; then
    BENCH_ARGS=(-i 1 dhry2reg whetstone-double)
    SUITE="fast"
else
    BENCH_ARGS=(-i 3)
    SUITE="full"
fi
echo "[*] Suite: $SUITE  BENCH_ARGS: ${BENCH_ARGS[*]}" >&2

BENCH_CMD="cd ${UNIXBENCH_DIR} && ${UNIXBENCH_BIN} ${BENCH_ARGS[*]}"
RUN_ROOT="${RUN_ROOT:-${REPO}/data/runs/unixbench-$(date +%Y%m%d-%H%M%S)}"
IFS=',' read -r -a MODES <<< "${MODES_CSV}"

if [[ "$DRY_RUN" == "1" ]]; then
    EXPANDED=()
    for m in "${MODES[@]}"; do
        EXPANDED+=("$m")
        [[ "$m" != "workload_only" ]] && EXPANDED+=("${m}_dry")
    done
    MODES=("${EXPANDED[@]}")
elif [[ "$DRY_RUN_ONLY" == "1" ]]; then
    EXPANDED=()
    for m in "${MODES[@]}"; do
        if [[ "$m" == "workload_only" ]]; then
            EXPANDED+=("$m")
        else
            EXPANDED+=("${m}_dry")
        fi
    done
    MODES=("${EXPANDED[@]}")
fi
UV_BIN="$(command -v uv || true)"
if [[ -z "${UV_BIN}" ]]; then
    echo "error: uv not found on PATH" >&2
    exit 1
fi

RESET_BETWEEN_BENCH="${REFLEX_RESET_BETWEEN_BENCH:-1}"
declare -A BASE_SYSCTLS=()
declare -a RESET_SYSCTLS=()

mkdir -p "$REPO/data" "$RUN_ROOT"

sysctl_to_proc_path() {
    local name="$1"
    echo "/proc/sys/${name//./\/}"
}

load_reset_sysctls() {
    mapfile -t RESET_SYSCTLS < <(
        "${UV_BIN}" run python - <<'PY'
try:
    from reflex.core.tuners.registry import TunerRegistry
except Exception:
    print("vm.swappiness")
    print("vm.dirty_ratio")
    print("vm.vfs_cache_pressure")
    raise SystemExit(0)

registry = TunerRegistry.default()
seen = set()
for tuner in registry.enabled_tuners():
    entry = getattr(tuner, "_entry", None)
    if entry is None:
        continue
    if getattr(entry, "scope", None) != "runtime_sysctl":
        continue
    sysctl = getattr(entry, "sysctl", "")
    if not sysctl or sysctl in seen:
        continue
    seen.add(sysctl)
    print(sysctl)
PY
    )

    if [[ "${#RESET_SYSCTLS[@]}" -eq 0 ]]; then
        RESET_SYSCTLS=("vm.swappiness" "vm.dirty_ratio" "vm.vfs_cache_pressure")
    fi

    # Keep dirty_bytes untouched during benchmark resets. dirty_ratio and
    # dirty_bytes are mode-coupled in the kernel, and forcing dirty_bytes back
    # can fail or create noisy restore warnings on some systems.
    local filtered=()
    local name
    for name in "${RESET_SYSCTLS[@]}"; do
        [[ "$name" == "vm.dirty_bytes" ]] && continue
        filtered+=("$name")
    done
    RESET_SYSCTLS=("${filtered[@]}")
}

snapshot_baseline_sysctls() {
    load_reset_sysctls
    for name in "${RESET_SYSCTLS[@]}"; do
        local path
        path="$(sysctl_to_proc_path "$name")"
        local value
        value="$(sudo cat "$path" 2>/dev/null || true)"
        if [[ -n "$value" ]]; then
            BASE_SYSCTLS["$name"]="$value"
        fi
    done
}

restore_baseline_sysctls() {
    restore_one_sysctl() {
        local name="$1"
        local value="$2"
        if command -v sysctl >/dev/null 2>&1; then
            if ! sudo sysctl -q -w "${name}=${value}" >/dev/null 2>&1; then
                echo "warn: could not restore ${name}=${value}; keeping current value" >&2
            fi
            return
        fi

        local path
        path="$(sysctl_to_proc_path "$name")"
        if ! printf '%s\n' "$value" | sudo tee "$path" >/dev/null 2>/dev/null; then
            echo "warn: could not restore ${name}=${value}; keeping current value" >&2
        fi
    }

    local name
    for name in "${RESET_SYSCTLS[@]}"; do
        if [[ -v BASE_SYSCTLS["$name"] ]]; then
            restore_one_sysctl "$name" "${BASE_SYSCTLS[$name]}"
        fi
    done
}

if [[ "$RESET_BETWEEN_BENCH" == "1" ]]; then
    snapshot_baseline_sysctls
    trap restore_baseline_sysctls EXIT
fi

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

run_mode_once() {
    local mode="$1"
    local run_dir="$2"
    local tag="$3"
    local log
    mkdir -p "$run_dir"
    log="$run_dir/workload${tag}.log"

    if [[ "$RESET_BETWEEN_BENCH" == "1" ]]; then
        restore_baseline_sysctls
    fi

    # Strip _dry suffix to get the real implementation name
    local impl_mode="$mode"
    local dry_flag=()
    if [[ "$mode" == *_dry ]]; then
        impl_mode="${mode%_dry}"
        dry_flag=(--dry-run)
    fi

    echo "[*] Running mode: $mode" >&2

    # Clean cgroup file
    sudo rm -f /tmp/reflex_cgroups
    sudo touch /tmp/reflex_cgroups
    sudo chmod 666 /tmp/reflex_cgroups

    if [[ "$impl_mode" == "workload_only" ]]; then
        run_unixbench "$log"
        printf '{"mode":"%s","run_dir":"%s","bench_cmd":"%s","tag":"%s","suite":"%s"}\n' \
            "$mode" "$run_dir" "$BENCH_CMD" "$tag" "$SUITE" > "$run_dir/run_metadata${tag}.json"
    else
        # 1. Create cgroup and pass its id directly to the implementation loader.
        local cgdir="/sys/fs/cgroup/reflex_ubench_$$"
        sudo mkdir -p "$cgdir"
        local cgid
        cgid=$(stat -c %i "$cgdir")

        local openai_env=()
        if [[ -n "${OPENAI_API_KEY:-}" ]]; then
            openai_env=("OPENAI_API_KEY=${OPENAI_API_KEY}")
        fi

        # 2. Start daemon_core implementation.
        sudo env "PATH=${PATH}" "UV_CACHE_DIR=${UV_CACHE_DIR:-/tmp/uv-cache}" \
            "${openai_env[@]}" \
            "${UV_BIN}" run reflex \
            --no-sudo \
            --run-id "unixbench-$mode" \
            --run-dir "$run_dir" \
            "${dry_flag[@]}" \
            "$impl_mode" \
            --cgroup-id "$cgid" \
            > "$run_dir/daemon${tag}.log" 2>&1 &
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
        printf '{"mode":"%s","run_dir":"%s","bench_cmd":"%s","tag":"%s","suite":"%s"}\n' \
            "$mode" "$run_dir" "$BENCH_CMD" "$tag" "$SUITE" > "$run_dir/run_metadata${tag}.json"
    fi

    local score
    score=$(parse_score "$log")
    if [[ -z "$score" ]]; then
        echo "error: could not parse UnixBench score for mode=$mode" >&2
        echo "  log: $log" >&2
        echo "--- workload log tail ---" >&2
        tail -80 "$log" >&2 || true
        echo "--- daemon log tail ---" >&2
        tail -80 "$run_dir/daemon${tag}.log" >&2 || true
        return 1
    fi
    echo "$score"
}

median_score() {
    "${UV_BIN}" run python - "$@" <<'PY'
import sys
vals = [float(v) for v in sys.argv[1:]]
vals.sort()
n = len(vals)
if n == 0:
    raise SystemExit(1)
if n % 2:
    print(f"{vals[n//2]:.1f}")
else:
    print(f"{((vals[n//2-1] + vals[n//2]) / 2.0):.1f}")
PY
}

run_bench() {
    local mode="$1"
    local run_dir="$RUN_ROOT/$mode"
    mkdir -p "$run_dir"

    local i
    for ((i=1; i<=WARMUP; i++)); do
        echo "[*] Warmup mode: $mode ($i/$WARMUP)" >&2
        run_mode_once "$mode" "$run_dir" ".warmup${i}" >/dev/null
    done

    local -a scores=()
    for ((i=1; i<=TRIALS; i++)); do
        echo "[*] Running mode: $mode (trial $i/$TRIALS)" >&2
        scores+=("$(run_mode_once "$mode" "$run_dir" ".trial${i}")")
    done

    local med
    med="$(median_score "${scores[@]}")"
    printf '%s\n' "${scores[@]}" > "$run_dir/trial_scores.txt"
    echo "    trial scores: ${scores[*]}" >&2
    echo "    median score: $med" >&2
    echo "$med"
}

echo "mode,score" > "$RUN_ROOT/unixbench_results.csv"

if [[ "$RANDOMIZE_ORDER" == "1" ]]; then
    mapfile -t MODES < <(printf '%s\n' "${MODES[@]}" | "${UV_BIN}" run python -c 'import random,sys; modes=[line.strip() for line in sys.stdin if line.strip()]; random.shuffle(modes); print("\n".join(modes))')
    echo "[*] Randomized mode order: ${MODES[*]}" >&2
fi

for mode in "${MODES[@]}"; do
    score=$(run_bench "$mode")
    echo "$mode,$score" >> "$RUN_ROOT/unixbench_results.csv"
done

echo ""
echo "Run artifacts written to $RUN_ROOT"
echo ""
cat "$RUN_ROOT/unixbench_results.csv"
