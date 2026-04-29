CLANG ?= clang
BPFTOOL ?= bpftool

BUILD_DIR := build
SRC_ROOT := ../..
LOADER := $(BUILD_DIR)/reflex
COLLECTOR := $(BUILD_DIR)/reflex.bpf.o
SKEL_H := $(BUILD_DIR)/reflex.skel.h

all: $(COLLECTOR) $(LOADER)

$(BUILD_DIR):
	mkdir -p $(BUILD_DIR)

$(COLLECTOR): reflex.bpf.c | $(BUILD_DIR)
	$(CLANG) -g -O2 -target bpf -I $(SRC_ROOT)/src -c $< -o $@

$(SKEL_H): $(COLLECTOR)
	$(BPFTOOL) gen skeleton $< > $@

$(LOADER): reflex.c $(SKEL_H)
	$(CLANG) -O2 -g -Wall -I/usr/include -I/usr/include/bpf -I./build -I$(SRC_ROOT)/src -o $@ $< -lbpf

clean:
	rm -rf $(BUILD_DIR)
