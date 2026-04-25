#!/bin/bash
# Usage:
#   ./run.sh [--io] [--cpu] [--mem]
#   ./run.sh --stressor "CMD ARGS"   # arbitrary workload in its own cgroup
#
# Examples:
#   sudo ./run.sh --mem
#   sudo ./run.sh --stressor "stress-ng --vm 2 --vm-bytes 75% --vm-keep"
#   sudo ./run.sh --stressor "fio --name=test --rw=randrw --bs=4k --size=2G --time_based --runtime=300 --filename=/tmp/fio_test"

RUN_IO=0
RUN_CPU=0
RUN_MEM=0
STRESSOR_CMD=""

for arg in "$@"; do
    case $arg in
        --io)  RUN_IO=1 ;;
        --cpu) RUN_CPU=1 ;;
        --mem) RUN_MEM=1 ;;
        --stressor)
            # handled below with shift-style parsing
            ;;
        *) ;;
    esac
done

# Re-parse properly to handle --stressor "CMD"
while [[ $# -gt 0 ]]; do
    case $1 in
        --io)       RUN_IO=1;          shift ;;
        --cpu)      RUN_CPU=1;         shift ;;
        --mem)      RUN_MEM=1;         shift ;;
        --stressor) STRESSOR_CMD="$2"; shift 2 ;;
        *) echo "Unknown argument: $1"; exit 1 ;;
    esac
done

mkdir -p build

# Generate skeleton if stale
if [ ! -f "build/collector.skel.h" ] || [ "build/collector.bpf.o" -nt "build/collector.skel.h" ]; then
    bpftool gen skeleton build/collector.bpf.o > build/collector.skel.h
fi

if [ ! -f "src/vmlinux.h" ]; then
    if [ -f "/sys/kernel/btf/vmlinux" ]; then
        bpftool btf dump file /sys/kernel/btf/vmlinux format c > src/vmlinux.h
    else
        echo "Error - kernel btf not found"
        exit 1
    fi
fi

make

CGROUP_IDS=()
TESTER_PIDS=()
CGROUP_DIRS=()

cleanup() {
    for pid in "${TESTER_PIDS[@]}"; do
        kill "$pid" 2>/dev/null
    done
    sleep 1
    for dir in "${CGROUP_DIRS[@]}"; do
        [ -d "$dir" ] && sudo rmdir "$dir" 2>/dev/null
    done
}
trap cleanup EXIT

launch_in_cgroup() {
    local binary=$1
    local cgdir=$2

    sudo mkdir -p "$cgdir"
    local cgid
    cgid=$(stat -c %i "$cgdir")
    CGROUP_IDS+=("$cgid")
    CGROUP_DIRS+=("$cgdir")

    "$binary" &
    local pid=$!
    TESTER_PIDS+=("$pid")
    echo "$pid" | sudo tee "$cgdir/cgroup.procs" > /dev/null
    echo "Launched $binary (pid=$pid, cgid=$cgid)"
}

launch_cmd_in_cgroup() {
    local cmd="$1"
    local cgdir=$2

    sudo mkdir -p "$cgdir"
    local cgid
    cgid=$(stat -c %i "$cgdir")
    CGROUP_IDS+=("$cgid")
    CGROUP_DIRS+=("$cgdir")

    bash -c "$cmd" &
    local pid=$!
    TESTER_PIDS+=("$pid")
    echo "$pid" | sudo tee "$cgdir/cgroup.procs" > /dev/null
    echo "Launched stressor (pid=$pid, cgid=$cgid): $cmd"
}

if [ $RUN_IO -eq 1 ]; then
    launch_in_cgroup ./build/tester_io /sys/fs/cgroup/reflex_io
fi

if [ $RUN_CPU -eq 1 ]; then
    launch_in_cgroup ./build/tester_cpu /sys/fs/cgroup/reflex_cpu
fi

if [ $RUN_MEM -eq 1 ]; then
    launch_in_cgroup ./build/tester_mem /sys/fs/cgroup/reflex_mem
fi

if [ -n "$STRESSOR_CMD" ]; then
    launch_cmd_in_cgroup "$STRESSOR_CMD" /sys/fs/cgroup/reflex_stressor
fi

if [ ${#CGROUP_IDS[@]} -eq 0 ]; then
    echo "No workload specified. Use --io, --cpu, --mem, or --stressor \"CMD\"."
    exit 1
fi

VENV_PYTHON="$(dirname "$0")/.venv/bin/python3"
PYTHON="${VENV_PYTHON:-python3}"
sudo "$PYTHON" daemon/main.py --cgroup-ids "${CGROUP_IDS[@]}"
