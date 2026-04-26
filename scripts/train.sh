#!/usr/bin/env bash
# train.sh — Run all workloads through the BO tuning loop.
#
# Usage:
#   sudo ./scripts/train.sh [options]
#
# Options:
#   -n N    BO experiments per workload (default: 25)
#   -d S    Measurement window seconds (default: 45)
#   -w S    Stressor ramp-up seconds (default: 10)
#   -s S    Cool-down between experiments (default: 8)
#   -l LIST Comma-separated workload numbers, e.g. 1,3,6-10
#   -x      Skip sysctl writes, stressor, and daemon (loop test only)
#   -r      Reset models (delete experiments.jsonl, library.json, gp_*.pkl)
#   -h      Show this message
#
# Required:   sudo apt install -y stress-ng fio sysbench gcc
# Optional:   sudo apt install -y ffmpeg imagemagick nginx apache2-utils
#             (wrk: https://github.com/wg/wrk — not in apt on all distros)
#
# Estimated time with defaults (25 experiments x 45s window):
#   each workload ≈ 28 min  →  25 workloads ≈ 12 hours
#   Run overnight, or pick a subset with -l 1,6,11,21,22

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"
PYTHON="${REPO_ROOT}/.venv/bin/python3"
TUNER="${SCRIPT_DIR}/tune_experiment.py"

NCPU=$(nproc)

# RAM-aware fio file size: needs to exceed page cache to test real disk.
RAM_KB=$(awk '/MemTotal/ { print $2 }' /proc/meminfo)
RAM_GB=$(( RAM_KB / 1024 / 1024 ))
FIO_GB=$(( RAM_GB * 2 ))
[[ $FIO_GB -lt 4 ]] && FIO_GB=4
FIO_SIZE="${FIO_GB}G"
FIO_FILE="/tmp/reflex_fio_test"

# Temp paths used by real-workload setup
COMPILE_SRC="/tmp/reflex_compile_src.c"
COMPILE_BIN="/tmp/reflex_compile_bin"
SYSBENCH_DB="/tmp/reflex_sysbench.db"
NGINX_PORT=18080
NGINX_CONF="/tmp/reflex_nginx.conf"
NGINX_PID="/tmp/reflex_nginx.pid"
BLENDER_SCENE="/tmp/reflex_blender_bench.blend"

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------
EXPERIMENTS=25
DURATION=45
WARMUP=10
SETTLE=8
DRY_RUN_FLAG=""
WORKLOAD_FILTER=""

# ---------------------------------------------------------------------------
# Argument parsing  (-n experiments  -d duration  -w warmup  -s settle
#                    -l workloads    -x dry-run   -r reset   -h help)
# ---------------------------------------------------------------------------
while getopts "n:d:w:s:l:rxh" opt; do
    case $opt in
        n) EXPERIMENTS="$OPTARG" ;;
        d) DURATION="$OPTARG" ;;
        w) WARMUP="$OPTARG" ;;
        s) SETTLE="$OPTARG" ;;
        l) WORKLOAD_FILTER="$OPTARG" ;;
        x) DRY_RUN_FLAG="--dry-run" ;;
        r) rm -f "${REPO_ROOT}/models/experiments.jsonl" "${REPO_ROOT}/models/library.json" "${REPO_ROOT}/models/gp_"*.pkl ;;
        h) sed -n '2,14p' "$0" | sed 's/^# \?//'; exit 0 ;;
        *) exit 1 ;;
    esac
done

# ---------------------------------------------------------------------------
# Dependency check
# ---------------------------------------------------------------------------
HAVE_FFMPEG=0; HAVE_CONVERT=0; HAVE_NGINX=0; HAVE_WRK=0; HAVE_AB=0; HAVE_BLENDER=0

check_deps() {
    local missing=()
    for cmd in stress-ng fio sysbench gcc; do
        command -v "$cmd" &>/dev/null || missing+=("$cmd")
    done
    if [[ ${#missing[@]} -gt 0 ]]; then
        echo "Missing required tools: ${missing[*]}"
        echo "Install with: sudo apt install -y ${missing[*]}"
        exit 1
    fi
    if [[ ! -x "$PYTHON" ]]; then
        echo "Python not found at $PYTHON"
        echo "Run: python3 -m venv .venv && .venv/bin/pip install -r requirements.txt"
        exit 1
    fi

    command -v ffmpeg  &>/dev/null && HAVE_FFMPEG=1   || echo "  [optional] ffmpeg not found — workload 20 will be skipped"
    command -v convert &>/dev/null && HAVE_CONVERT=1  || echo "  [optional] imagemagick not found — workload 23 will be skipped"
    command -v nginx   &>/dev/null && HAVE_NGINX=1    || echo "  [optional] nginx not found — workload 24 will be skipped"
    command -v blender &>/dev/null && HAVE_BLENDER=1  || echo "  [optional] blender not found — workload 26 will be skipped"
    echo "  Install blender: sudo apt install -y blender  OR  sudo snap install blender --classic"
    command -v wrk     &>/dev/null && HAVE_WRK=1
    command -v ab      &>/dev/null && HAVE_AB=1
    if [[ $HAVE_NGINX -eq 1 && $HAVE_WRK -eq 0 && $HAVE_AB -eq 0 ]]; then
        echo "  [optional] nginx found but neither wrk nor ab (apache2-utils) found — workload 24 will be skipped"
        HAVE_NGINX=0
    fi
}

# ---------------------------------------------------------------------------
# One-time setup helpers
# ---------------------------------------------------------------------------

prepare_fio() {
    [[ -n "$DRY_RUN_FLAG" ]] && return
    if [[ ! -f "$FIO_FILE" ]]; then
        echo "Preparing fio test file (${FIO_SIZE}) — larger than RAM to bypass page cache..."
        fio --name=prep --filename="$FIO_FILE" --size="$FIO_SIZE" \
            --rw=write --bs=1M --numjobs=1 --end_fsync=1 \
            --output=/dev/null
        echo "  done: $FIO_FILE"
    else
        local actual_bytes
        actual_bytes=$(stat -c %s "$FIO_FILE" 2>/dev/null || echo 0)
        local want_bytes=$(( FIO_GB * 1024 * 1024 * 1024 ))
        if (( actual_bytes < want_bytes / 2 )); then
            echo "Existing fio file is too small ($(( actual_bytes / 1024 / 1024 ))M vs ${FIO_SIZE} needed). Re-creating..."
            rm -f "$FIO_FILE"
            fio --name=prep --filename="$FIO_FILE" --size="$FIO_SIZE" \
                --rw=write --bs=1M --numjobs=1 --end_fsync=1 \
                --output=/dev/null
        else
            echo "  fio test file OK: $FIO_FILE ($(( actual_bytes / 1024 / 1024 / 1024 ))G)"
        fi
    fi
}

prepare_compile_src() {
    [[ -n "$DRY_RUN_FLAG" ]] && return
    if [[ ! -f "$COMPILE_SRC" ]]; then
        echo "Generating compile source ($COMPILE_SRC)..."
        python3 - <<'PYEOF' > "$COMPILE_SRC"
import math
lines = ["#include <math.h>", "#include <stdio.h>"]
for i in range(4000):
    lines.append(
        f"static double f{i}(double x, double y) "
        f"{{ return sin(x+{i})*cos(y/{i+1}+1.0)*log(fabs(x*y+{i})+1.0); }}"
    )
lines += [
    "int main(void) {",
    "  double s = 0.0; long i;",
    "  for (i = 0; i < 20000000L; i++) {",
]
for i in range(20):
    lines.append(f"    s += f{i}((double)i, s);")
lines += ["  }", "  printf(\"%f\\n\", s); return 0; }"]
print("\n".join(lines))
PYEOF
        echo "  done: $COMPILE_SRC ($(wc -l < "$COMPILE_SRC") lines)"
    else
        echo "  compile source OK: $COMPILE_SRC"
    fi
}

prepare_sysbench_db() {
    [[ -n "$DRY_RUN_FLAG" ]] && return
    if [[ ! -f "$SYSBENCH_DB" ]]; then
        echo "Preparing sysbench OLTP database ($SYSBENCH_DB)..."
        sysbench --db-driver=sqlite --sqlite-db="$SYSBENCH_DB" \
            oltp_read_write --tables=4 --table-size=20000 prepare \
            2>/dev/null
        echo "  done: $SYSBENCH_DB"
    else
        echo "  sysbench DB OK: $SYSBENCH_DB"
    fi
}

prepare_nginx() {
    [[ $HAVE_NGINX -eq 0 ]] && return
    [[ -n "$DRY_RUN_FLAG" ]] && return
    cat > "$NGINX_CONF" <<EOF
worker_processes $NCPU;
events { worker_connections 1024; }
http {
    access_log off;
    server {
        listen $NGINX_PORT;
        location / { return 200 "ok\n"; }
    }
}
EOF
    echo "  nginx config written: $NGINX_CONF"
}

prepare_blender_scene() {
    [[ $HAVE_BLENDER -eq 0 ]] && return
    [[ -n "$DRY_RUN_FLAG" ]] && return
    if [[ ! -f "$BLENDER_SCENE" ]]; then
        echo "Generating Blender benchmark scene ($BLENDER_SCENE)..."
        # Build the scene entirely from Blender's Python API — no .blend download needed.
        blender --background --python-expr "
import bpy
bpy.ops.wm.read_factory_settings(use_empty=True)
bpy.ops.mesh.primitive_ico_sphere_add(subdivisions=7, radius=2)
ob = bpy.context.active_object
bpy.ops.object.modifier_add(type='SUBSURF')
ob.modifiers['Subdivision'].levels = 2
bpy.ops.object.light_add(type='SUN', location=(5, 5, 10))
bpy.ops.object.camera_add(location=(0, -5, 2))
bpy.context.scene.camera = bpy.context.active_object
scene = bpy.context.scene
scene.render.engine = 'CYCLES'
scene.cycles.device = 'CPU'
scene.cycles.samples = 128
scene.render.resolution_x = 1280
scene.render.resolution_y = 720
bpy.ops.wm.save_as_mainfile(filepath='$BLENDER_SCENE')
print('Scene saved.')
" 2>/dev/null
        echo "  done: $BLENDER_SCENE"
    else
        echo "  Blender scene OK: $BLENDER_SCENE"
    fi
}

cleanup() {
    # Stop nginx if we started it
    if [[ -f "$NGINX_PID" ]]; then
        kill "$(cat "$NGINX_PID")" 2>/dev/null || true
        rm -f "$NGINX_PID"
    fi
    rm -f "$FIO_FILE" "$COMPILE_SRC" "$COMPILE_BIN" \
          "$SYSBENCH_DB" "$NGINX_CONF" "$BLENDER_SCENE" \
          /tmp/reflex_img.png /tmp/reflex_compile_bin_* 2>/dev/null || true
    echo "Cleaned up temp files."
}
trap cleanup EXIT

# ---------------------------------------------------------------------------
# Workload definitions
# ---------------------------------------------------------------------------

declare -a WL_NAMES
declare -a WL_CMDS
declare -a WL_SKIP    # 1 = skip this workload

# ---- Memory workloads (1-5) ------------------------------------------------

WL_NAMES[1]="vm-moderate"
WL_CMDS[1]="stress-ng --vm 2 --vm-bytes 60% --vm-keep"
WL_SKIP[1]=0

WL_NAMES[2]="vm-heavy"
WL_CMDS[2]="stress-ng --vm 4 --vm-bytes 80% --vm-keep"
WL_SKIP[2]=0

WL_NAMES[3]="vm-extreme"
# --oom-avoid backs off before triggering OOM — keeps results stable
WL_CMDS[3]="stress-ng --vm 2 --vm-bytes 90% --vm-keep --oom-avoid"
WL_SKIP[3]=0

WL_NAMES[4]="mem-bandwidth-seq"
WL_CMDS[4]="sysbench memory --memory-block-size=1M --memory-total-size=200G run"
WL_SKIP[4]=0

WL_NAMES[5]="mem-bandwidth-rnd"
WL_CMDS[5]="sysbench memory --memory-block-size=64 --memory-access-mode=rnd --memory-total-size=100G run"
WL_SKIP[5]=0

# ---- CPU workloads (6-10) --------------------------------------------------

WL_NAMES[6]="cpu-matrixprod"
WL_CMDS[6]="stress-ng --cpu $NCPU --cpu-method matrixprod"
WL_SKIP[6]=0

WL_NAMES[7]="cpu-fft"
WL_CMDS[7]="stress-ng --cpu $NCPU --cpu-method fft"
WL_SKIP[7]=0

WL_NAMES[8]="cpu-bitops"
WL_CMDS[8]="stress-ng --cpu $NCPU --cpu-method bitops"
WL_SKIP[8]=0

WL_NAMES[9]="cpu-primes"
WL_CMDS[9]="sysbench cpu --cpu-max-prime=50000 --threads=$NCPU --time=300 run"
WL_SKIP[9]=0

WL_NAMES[10]="cpu-ipc"
# Drives context-switch rate high: many processes passing messages through pipes
WL_CMDS[10]="stress-ng --pipe $NCPU --pipe-size 64"
WL_SKIP[10]=0

# ---- I/O workloads (11-15) — --direct=1 bypasses page cache ---------------

WL_NAMES[11]="io-randread-4k"
WL_CMDS[11]="fio --name=randread --rw=randread --bs=4k --size=${FIO_SIZE} --numjobs=4 --iodepth=32 --direct=1 --time_based --runtime=300 --group_reporting --filename=${FIO_FILE}"
WL_SKIP[11]=0

WL_NAMES[12]="io-randwrite-4k"
WL_CMDS[12]="fio --name=randwrite --rw=randwrite --bs=4k --size=${FIO_SIZE} --numjobs=4 --iodepth=32 --direct=1 --time_based --runtime=300 --group_reporting --filename=${FIO_FILE}"
WL_SKIP[12]=0

WL_NAMES[13]="io-randrw-4k"
WL_CMDS[13]="fio --name=randrw --rw=randrw --bs=4k --size=${FIO_SIZE} --numjobs=4 --iodepth=16 --direct=1 --time_based --runtime=300 --group_reporting --filename=${FIO_FILE}"
WL_SKIP[13]=0

WL_NAMES[14]="io-seqread-1m"
WL_CMDS[14]="fio --name=seqread --rw=read --bs=1M --size=${FIO_SIZE} --numjobs=2 --iodepth=4 --direct=1 --time_based --runtime=300 --group_reporting --filename=${FIO_FILE}"
WL_SKIP[14]=0

WL_NAMES[15]="io-seqwrite-64k"
WL_CMDS[15]="fio --name=seqwrite --rw=write --bs=64k --size=${FIO_SIZE} --numjobs=2 --iodepth=8 --direct=1 --time_based --runtime=300 --group_reporting --filename=${FIO_FILE}"
WL_SKIP[15]=0

# ---- Mixed / synthetic (16-20) ---------------------------------------------

WL_NAMES[16]="mixed-compile-synthetic"
WL_CMDS[16]="stress-ng --cpu $NCPU --vm 2 --vm-bytes 40% --vm-keep"
WL_SKIP[16]=0

WL_NAMES[17]="mixed-desktop"
WL_CMDS[17]="stress-ng --cpu 2 --vm 2 --vm-bytes 50% --vm-keep --iomix 2"
WL_SKIP[17]=0

WL_NAMES[18]="mixed-video-edit"
WL_CMDS[18]="stress-ng --vm 2 --vm-bytes 70% --vm-keep --hdd 2"
WL_SKIP[18]=0

WL_NAMES[19]="mixed-webserver-synthetic"
WL_CMDS[19]="stress-ng --cpu 4 --vm 1 --vm-bytes 30% --vm-keep --pipe 4"
WL_SKIP[19]=0

WL_NAMES[20]="encode-h264"
# Actual H.264 encode — real CPU+memory pattern, not stress-ng abstraction
if [[ $HAVE_FFMPEG -eq 1 ]]; then
    WL_CMDS[20]="ffmpeg -f lavfi -i testsrc=duration=3600:size=1920x1080:rate=30 -vcodec libx264 -preset medium -f null /dev/null"
    WL_SKIP[20]=0
else
    WL_CMDS[20]="stress-ng --cpu $NCPU --cpu-method matrixprod"
    WL_SKIP[20]=1
fi

# ---- Real workloads (21-25) — produce genuinely different eBPF signatures --

WL_NAMES[21]="real-compile"
# Parallel gcc invocations — generates fork/exec cascade, linker I/O, many
# short-lived processes. context_switch_rate and process_churn spike in eBPF.
# Nothing in workloads 1-20 produces this pattern.
WL_CMDS[21]="bash -c 'while true; do for i in \$(seq 1 $NCPU); do gcc -O2 -march=native -o ${COMPILE_BIN}_\$i $COMPILE_SRC -lm 2>/dev/null & done; wait; done'"
WL_SKIP[21]=0

WL_NAMES[22]="real-oltp-sqlite"
# sysbench OLTP on SQLite: lock contention, page cache under write pressure,
# fsync() calls. Direct I/O would bypass cache; we leave it buffered because
# real databases use the page cache. Very different from fio --direct=1.
WL_CMDS[22]="sysbench --db-driver=sqlite --sqlite-db=${SYSBENCH_DB} oltp_read_write --tables=4 --threads=$NCPU --time=300 run"
WL_SKIP[22]=0

WL_NAMES[23]="real-imagemagick"
# ImageMagick plasma fractal + blur: realistic photo editing profile.
# Heavy memory allocations, some CPU, occasional disk write. If not installed,
# falls back to stress-ng cpu+vm which approximates the same signature.
if [[ $HAVE_CONVERT -eq 1 ]]; then
    WL_CMDS[23]="bash -c 'while true; do convert -size 2048x2048 plasma:fractal -blur 0x6 -sharpen 0x2 /tmp/reflex_img.png 2>/dev/null || true; done'"
    WL_SKIP[23]=0
else
    WL_CMDS[23]="stress-ng --cpu $NCPU --vm 2 --vm-bytes 55% --vm-keep"
    WL_SKIP[23]=1
fi

WL_NAMES[24]="real-http-server"
# nginx under HTTP load: epoll, accept loops, network wakeups, short-lived
# connections. This is the only workload that produces real network syscall
# patterns in the eBPF data — nothing in 1-23 does this.
if [[ $HAVE_NGINX -eq 1 ]]; then
    # Pick http load tool: wrk preferred, ab as fallback
    if [[ $HAVE_WRK -eq 1 ]]; then
        HTTP_LOAD="wrk -t$NCPU -c256 -d300s http://127.0.0.1:${NGINX_PORT}/"
    else
        HTTP_LOAD="ab -n 10000000 -c 128 -k http://127.0.0.1:${NGINX_PORT}/"
    fi
    # Wrap in a script: start nginx, run load, stop nginx on exit
    WL_CMDS[24]="bash -c 'nginx -c $NGINX_CONF -g \"pid $NGINX_PID; daemon off;\" & sleep 1; $HTTP_LOAD; kill \$(cat $NGINX_PID) 2>/dev/null || true'"
    WL_SKIP[24]=0
else
    WL_CMDS[24]="stress-ng --cpu 4 --pipe 8"
    WL_SKIP[24]=1
fi

WL_NAMES[25]="real-bg-latency"
# Latency-under-load: background CPU hog competes with repeated short tasks.
# Exercises the scheduler's ability to give foreground tasks CPU time.
# The sched_cfs_bandwidth_slice_us knob matters most here.
WL_CMDS[25]="bash -c 'stress-ng --cpu $NCPU --cpu-method matrixprod &
BG_PID=\$!
trap \"kill \$BG_PID 2>/dev/null\" EXIT
while true; do
  for i in \$(seq 1 200); do
    sqlite3 /tmp/reflex_bg_lat.db \"CREATE TABLE IF NOT EXISTS t(v); INSERT INTO t VALUES(\$RANDOM); SELECT COUNT(*) FROM t;\" >/dev/null 2>&1 || true
  done
done'"
WL_SKIP[25]=0

WL_NAMES[26]="real-blender-cycles"
# Blender Cycles CPU render: multi-threaded ray tracing, realistic memory
# access patterns (BVH traversal), very different from stress-ng matrixprod.
if [[ $HAVE_BLENDER -eq 1 ]]; then
    WL_CMDS[26]="bash -c 'while true; do blender -b $BLENDER_SCENE -f 1 -o /tmp/reflex_render 2>/dev/null || true; done'"
    WL_SKIP[26]=0
else
    WL_CMDS[26]="stress-ng --cpu $NCPU --cpu-method matrixprod"
    WL_SKIP[26]=1
fi

# ---------------------------------------------------------------------------
# Workload filter parser (supports 1,3,6-10 syntax)
# ---------------------------------------------------------------------------
should_run() {
    local n="$1"
    [[ "${WL_SKIP[$n]:-0}" -eq 1 ]] && return 1
    [[ -z "$WORKLOAD_FILTER" ]] && return 0
    IFS=',' read -ra parts <<< "$WORKLOAD_FILTER"
    for part in "${parts[@]}"; do
        if [[ "$part" == *-* ]]; then
            local lo="${part%-*}" hi="${part#*-}"
            (( n >= lo && n <= hi )) && return 0
        elif [[ "$part" == "$n" ]]; then
            return 0
        fi
    done
    return 1
}

# ---------------------------------------------------------------------------
# Run one workload through tune_experiment.py
# ---------------------------------------------------------------------------
run_workload() {
    local n="$1"
    local name="${WL_NAMES[$n]}"
    local cmd="${WL_CMDS[$n]}"

    echo ""
    echo "============================================================"
    printf "  Workload %2d/26 — %s\n" "$n" "$name"
    echo "  Stressor: $cmd"
    echo "============================================================"

    "$PYTHON" "$TUNER" \
        --stressor "$cmd" \
        --experiments "$EXPERIMENTS" \
        --duration   "$DURATION" \
        --warmup     "$WARMUP" \
        --settle     "$SETTLE" \
        --skip-cluster \
        $DRY_RUN_FLAG
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
echo "============================================================"
echo "  Reflex training session"
echo "  Experiments per workload : $EXPERIMENTS"
echo "  Measurement window       : ${DURATION}s"
echo "  Warmup                   : ${WARMUP}s"
echo "  Settle                   : ${SETTLE}s"
echo "  CPUs detected            : $NCPU"
echo "  RAM detected             : ${RAM_GB}G  →  fio test file: ${FIO_SIZE}"
[[ -n "$DRY_RUN_FLAG" ]] && echo "  *** DRY RUN — no sysctls written ***"
echo "============================================================"
echo ""

if [[ -z "$DRY_RUN_FLAG" && $EUID -ne 0 ]]; then
    echo "error: must run as root (sudo ./scripts/train.sh)" >&2
    exit 1
fi

check_deps

echo ""
echo "--- One-time setup ---"
prepare_fio
prepare_compile_src
prepare_sysbench_db
prepare_nginx
prepare_blender_scene
echo ""

# Count workloads to run
TOTAL=0
for i in $(seq 1 26); do
    should_run "$i" && TOTAL=$((TOTAL + 1))
done

EST_MIN=$(( TOTAL * EXPERIMENTS * (WARMUP + DURATION + 4 + SETTLE) / 60 ))
echo "Running $TOTAL workload(s).  Estimated time: ${EST_MIN} minutes."
echo "Results accumulate in models/experiments.jsonl — safe to Ctrl+C and resume."
echo ""

DONE=0
for i in $(seq 1 26); do
    should_run "$i" || continue
    DONE=$((DONE + 1))
    echo "--- Progress: workload $DONE / $TOTAL (workload #$i) ---"
    run_workload "$i"
done

echo ""
echo "--- Running k-means clustering over all ${EXPERIMENTS}-experiment sessions ---"
"$PYTHON" - <<PYEOF
import sys
sys.path.insert(0, "${REPO_ROOT}/daemon")
sys.path.insert(0, "${REPO_ROOT}/scripts")
from tune_experiment import cluster_and_save_library
from pathlib import Path
cluster_and_save_library(
    Path("${REPO_ROOT}/models/experiments.jsonl"),
    Path("${REPO_ROOT}/models/library.json"),
    catalog_path=Path("${REPO_ROOT}/configs/tuner_catalog.yaml"),
    max_k=8,
)
PYEOF

echo ""
echo "============================================================"
echo "  Training complete."
echo "  Library written to: ${REPO_ROOT}/models/library.json"
echo "  Reload the runtime daemon to pick up the new workload library."
echo "============================================================"
