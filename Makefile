# Reflex Makefile

# Clang, llvm, libbpf-dev, bpftool

# CLANG ?= clang



# CFLAGS := -g -Wall -I$(SRC_DIR) -I$(BUILD_DIR)

# LDFLAGS := -lbpf -lelf -lz #linker flags

# BPF_CFLAGS := -g -O2 -target bpf -D__TARGET_ARCH_x86 #for .bpf.c compiling

# # Create vmlinux.h if it doesnt exist yet
# VMLINUX_H := $(SRC_DIR)/vmlinux.h

# $(VMLINUX_H):
# 	@if [ -f /sys/kernel/btf/vmlinux ]; then \
# 		$(BPFTOOL) btf dump file /sys/kernel/btf/vmlinux format c > $@; \
# 	else \
# 		echo "ERROR: /sys/kernel/btf/vmlinux not found. Your kernel may not have BTF enabled."; \
# 		echo "Try: CONFIG_DEBUG_INFO_BTF=y in kernel config"; \
# 		exit 1; \
# 	fi

# vmlinux: $(VMLINUX_H)



# starter version, make the bpf object and loader separately

CLANG ?= clang

SRC_DIR := src
EBPF_DIR := ebpf
BUILD_DIR := build

LOADER := $(BUILD_DIR)/loader
COLLECTOR := $(BUILD_DIR)/collector.bpf.o
SKEL_H := $(BUILD_DIR)/collector.skel.h # new for skeleton to be generated in between
# ^ maybe move to src?


all: $(COLLECTOR) $(LOADER)

$(COLLECTOR): $(EBPF_DIR)/collector.bpf.c
	$(CLANG) -g -O2 -target bpf -I ./src -c $< -o $@

$(SKEL_H): $(COLLECTOR)
	bpftool gen skeleton $< > $@

$(LOADER): $(SRC_DIR)/loader.c $(SKEL_H)
	$(CLANG) -O2 -g -Wall -I/usr/include -I/usr/include/bpf -I./build -I./src -o $@ $< -lbpf

clean:
	rm -rf $(BUILD_DIR)

# add to test git