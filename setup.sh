# add dependencies

sudo apt update
sudo apt install -y \
  auditd \
  build-essential \
  clang llvm \
  libbpf-dev libelf-dev zlib1g-dev pkg-config \
  linux-headers-$(uname -r) \
  bpfcc-tools python3-bpfcc libbpfcc-dev \
  stress-ng fio sysbench ffmpeg1 blender # workloads