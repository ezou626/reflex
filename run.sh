#!/bin/bash

# make sure dependencies are installed like ausyscall
mkdir build
rm -f log

if [ "build/collector.bpf.o" -nt "collector.skel.h" ] || [ ! -f "collector.skel.h"]; then
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
# gcc -O2 tester.c -o /build/tester
# strace -c ./build/tester
make

# switched to python driving for now. Run from repo root
# cd build && sudo ./loader | python3 ../src/parser.py&

python3 src/parser.py > log