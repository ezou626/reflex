#!/bin/bash

# Usage: ./run.sh [--io] [--cpu] [--mem]
#   --io   launch I/O stress tester and whitelist its cgroup
#   --cpu  launch CPU/syscall stress tester and whitelist its cgroup
#   --mem  launch memory stress tester and whitelist its cgroup

RUN_IO=0
RUN_CPU=0
RUN_MEM=0

for arg in "$@"; do
    case $arg in
        --io)  RUN_IO=1 ;;
        --cpu) RUN_CPU=1 ;;
        --mem) RUN_MEM=1 ;;
        *) echo "Unknown argument: $arg"; exit 1 ;;
    esac
done

mkdir -p build
# rm -rf data/runs

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
    # Wait for procs to exit so cgroup dirs can be removed
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

if [ $RUN_IO -eq 1 ]; then
    launch_in_cgroup ./build/tester_io /sys/fs/cgroup/reflex_io
fi

if [ $RUN_CPU -eq 1 ]; then
    launch_in_cgroup ./build/tester_cpu /sys/fs/cgroup/reflex_cpu
fi

if [ $RUN_MEM -eq 1 ]; then
    launch_in_cgroup ./build/tester_mem /sys/fs/cgroup/reflex_mem
fi

python3 daemon/main.py --cgroup-ids "${CGROUP_IDS[@]}"
