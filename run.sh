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
make
cd build && sudo ./loader | python3 ../src/parser.py