#!/usr/bin/env bash
# Boot an Ubuntu cloud image under QEMU, mount this repo, build the local eBPF
# loader, install UnixBench if needed, and run benchmarks/unixbench_compare.sh
# inside the guest.
#
# Usage:
#   benchmarks/unixbench_qemu.sh
#
# Useful env:
#   REFLEX_VM_CACHE       cache directory (default: ./data/qemu)
#   REFLEX_VM_SSH_PORT    host TCP port forwarded to guest :22 (default: 52222)
#   REFLEX_VM_DISK_GB     guest root disk size in GiB (default: 24)
#   REFLEX_UNIXBENCH_IMAGE_URL  optional URL for prebaked unixbench image
#   REFLEX_FALLBACK_IMAGE_URL   fallback Ubuntu cloud image URL
#   UNIXBENCH_URL         git URL for UnixBench (default: kdlucas/byte-unixbench)
#   MODES                 comma modes for unixbench_compare.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="${SCRIPT_DIR%/benchmarks}"
cd "${REPO_ROOT}"

CACHE_DIR="${REFLEX_VM_CACHE:-${REPO_ROOT}/data/qemu}"
SSH_PORT="${REFLEX_VM_SSH_PORT:-52222}"
DISK_GB="${REFLEX_VM_DISK_GB:-24}"
BASE_NAME="${REFLEX_UNIXBENCH_IMAGE_NAME:-noble-unixbench-deps-amd64.img}"
BASE_URL="${REFLEX_UNIXBENCH_IMAGE_URL:-}"
FALLBACK_IMAGE_URL="${REFLEX_FALLBACK_IMAGE_URL:-https://cloud-images.ubuntu.com/noble/current/noble-server-cloudimg-amd64.img}"
BASE_IMG="${CACHE_DIR}/${BASE_NAME}"
OVERLAY="${CACHE_DIR}/unixbench-overlay-$$.qcow2"
SEED_ISO="${CACHE_DIR}/unixbench-seed-$$.img"
KEY="${CACHE_DIR}/id_ed25519"
PUB="${KEY}.pub"
CONSOLE_LOG="${CACHE_DIR}/unixbench-console-$$.log"
QEMU_PID_FILE="${CACHE_DIR}/unixbench-qemu-$$.pid"
KNOWN_HOSTS="${CACHE_DIR}/known_hosts.unixbench.$$"
UNIXBENCH_URL="${UNIXBENCH_URL:-https://github.com/kdlucas/byte-unixbench.git}"

need_cmd() {
  command -v "$1" >/dev/null 2>&1 || {
    echo "error: missing '$1'. Install it and retry." >&2
    exit 1
  }
}

cleanup() {
  if [[ -n "${QEMU_PID:-}" ]] && kill -0 "${QEMU_PID}" 2>/dev/null; then
    kill "${QEMU_PID}" 2>/dev/null || true
    wait "${QEMU_PID}" 2>/dev/null || true
  fi
  rm -f "${OVERLAY}" "${SEED_ISO}" "${CONSOLE_LOG}" "${QEMU_PID_FILE}" "${KNOWN_HOSTS}" 2>/dev/null || true
}
trap cleanup EXIT

need_cmd qemu-system-x86_64
need_cmd qemu-img
need_cmd ssh
need_cmd ssh-keygen

if [[ ! -e /dev/kvm ]] && command -v modprobe >/dev/null 2>&1; then
  if grep -Eq 'vmx|svm' /proc/cpuinfo 2>/dev/null; then
    if grep -Eq 'vmx' /proc/cpuinfo 2>/dev/null; then
      modprobe kvm_intel 2>/dev/null || true
    else
      modprobe kvm_amd 2>/dev/null || true
    fi
  fi
fi

if [[ ! -e /dev/kvm ]]; then
  cat >&2 <<'EOF'
error: /dev/kvm does not exist.

This host is not exposing KVM. Check:
  ls -l /dev/kvm
  grep -E 'vmx|svm' /proc/cpuinfo | head
  sudo modprobe kvm_intel   # Intel
  sudo modprobe kvm_amd     # AMD

If this host itself is a VM, nested virtualization may need to be enabled by
the host/hypervisor provider.
EOF
  exit 1
fi

if [[ ! -r /dev/kvm ]]; then
  echo "error: cannot read /dev/kvm. Add your user to kvm or run this script with sudo." >&2
  exit 1
fi

if ! command -v cloud-localds >/dev/null 2>&1; then
  echo "error: cloud-localds not found. Install: sudo apt install cloud-image-utils" >&2
  exit 1
fi

mkdir -p "${CACHE_DIR}" "${REPO_ROOT}/data"
rm -f "${CACHE_DIR}/known_hosts"

if [[ ! -f "${KEY}" ]]; then
  echo "[unixbench-qemu] generating SSH key: ${KEY}"
  ssh-keygen -t ed25519 -f "${KEY}" -N "" -q
fi

if [[ ! -f "${BASE_IMG}" ]]; then
  if [[ -n "${BASE_URL}" ]]; then
    echo "[unixbench-qemu] downloading VM base image ${BASE_URL}"
    if command -v curl >/dev/null 2>&1; then
      curl -fL --retry 3 -o "${BASE_IMG}.part" "${BASE_URL}"
    else
      wget -O "${BASE_IMG}.part" "${BASE_URL}"
    fi
    mv "${BASE_IMG}.part" "${BASE_IMG}"
  else
    echo "[unixbench-qemu] prebaked image missing; building it now via bake_unixbench_image.sh"
    BAKE_SSH_PORT="$((SSH_PORT + 100))"
    if [[ "${BAKE_SSH_PORT}" -gt 65535 ]]; then
      BAKE_SSH_PORT=52223
    fi
    REFLEX_VM_CACHE="${CACHE_DIR}" \
    REFLEX_VM_SSH_PORT="${BAKE_SSH_PORT}" \
    REFLEX_BAKE_SOURCE_URL="${FALLBACK_IMAGE_URL}" \
    REFLEX_BAKE_OUTPUT_IMG="${BASE_NAME}" \
      bash "${SCRIPT_DIR}/bake_unixbench_image.sh"
    if [[ ! -f "${BASE_IMG}" ]]; then
      echo "error: bake completed but image is still missing: ${BASE_IMG}" >&2
      exit 1
    fi
  fi
fi

META_DATA="$(mktemp)"
USER_DATA="$(mktemp)"
INSTANCE_ID="reflex-unixbench-$(date +%s)-$$"
cat >"${META_DATA}" <<EOF
instance-id: ${INSTANCE_ID}
local-hostname: reflex-unixbench
EOF

PUB_LINE="$(cat "${PUB}")"
cat >"${USER_DATA}" <<EOF
#cloud-config
package_update: false
growpart:
  mode: auto
  devices: ["/"]
  ignore_growroot_disabled: false
resize_rootfs: true
users:
  - name: ubuntu
    shell: /bin/bash
    groups: [adm, cdrom, dip, lxd, sudo]
    sudo: ALL=(ALL) NOPASSWD:ALL
    lock_passwd: true
    ssh_authorized_keys:
      - ${PUB_LINE}
EOF

cloud-localds "${SEED_ISO}" "${USER_DATA}" "${META_DATA}"
rm -f "${META_DATA}" "${USER_DATA}"

qemu-img create -f qcow2 -F qcow2 -b "$(realpath "${BASE_IMG}")" "${OVERLAY}"
qemu-img resize "${OVERLAY}" "${DISK_GB}G"

echo "[unixbench-qemu] starting QEMU on 127.0.0.1:${SSH_PORT}"
qemu-system-x86_64 \
  -machine type=q35,accel=kvm \
  -cpu host \
  -smp 2 \
  -m 4096 \
  -display none \
  -daemonize \
  -pidfile "${QEMU_PID_FILE}" \
  -serial file:"${CONSOLE_LOG}" \
  -drive file="${OVERLAY}",if=virtio,cache=writeback \
  -drive file="${SEED_ISO}",if=virtio,format=raw \
  -netdev user,id=net0,hostfwd=tcp:127.0.0.1:${SSH_PORT}-:22 \
  -device virtio-net-pci,netdev=net0 \
  -fsdev local,id=reflex_dev,path="${REPO_ROOT}",security_model=none \
  -device virtio-9p-pci,fsdev=reflex_dev,mount_tag=hostshare

if [[ ! -f "${QEMU_PID_FILE}" ]]; then
  echo "error: QEMU did not create ${QEMU_PID_FILE}" >&2
  tail -40 "${CONSOLE_LOG}" >&2 || true
  exit 1
fi
QEMU_PID="$(cat "${QEMU_PID_FILE}")"

touch "${KNOWN_HOSTS}"
chmod 600 "${KNOWN_HOSTS}" 2>/dev/null || true

echo "[unixbench-qemu] waiting for SSH"
READY=0
for _ in $(seq 1 120); do
  if ssh \
    -i "${KEY}" \
    -p "${SSH_PORT}" \
    -o StrictHostKeyChecking=accept-new \
    -o UserKnownHostsFile="${KNOWN_HOSTS}" \
    -o ConnectTimeout=5 \
    -o BatchMode=yes \
    ubuntu@127.0.0.1 \
    "echo ok" >/dev/null 2>&1; then
    READY=1
    break
  fi
  sleep 5
done

if [[ "${READY}" -ne 1 ]]; then
  echo "error: SSH did not become ready. Last console lines:" >&2
  tail -50 "${CONSOLE_LOG}" >&2 || true
  exit 1
fi

echo "[unixbench-qemu] running guest benchmark"
ssh \
  -i "${KEY}" \
  -p "${SSH_PORT}" \
  -o StrictHostKeyChecking=yes \
  -o UserKnownHostsFile="${KNOWN_HOSTS}" \
  -o ConnectTimeout=30 \
  ubuntu@127.0.0.1 \
  "UNIXBENCH_URL='${UNIXBENCH_URL}' MODES='${MODES:-workload_only,heuristic,classifier}' bash -s" <<'GUEST'
set -euo pipefail

wait_for_apt() {
  if command -v cloud-init >/dev/null 2>&1; then
    sudo cloud-init status --wait 2>/dev/null || true
  fi
  local n=0
  while sudo fuser /var/lib/apt/lists/lock /var/lib/dpkg/lock-frontend /var/lib/dpkg/lock >/dev/null 2>&1; do
    n=$((n + 1))
    if [[ "${n}" -gt 90 ]]; then
      echo "error: apt/dpkg locks still held" >&2
      exit 1
    fi
    sleep 2
  done
}
wait_for_apt
export PATH="$HOME/.local/bin:$PATH"

sudo modprobe 9pnet_virtio 2>/dev/null || true
sudo mkdir -p /mnt/reflex
if ! mountpoint -q /mnt/reflex; then
  sudo mount -t 9p -o trans=virtio,version=9p2000.L hostshare /mnt/reflex
fi

cd /mnt/reflex
for dep in uv make clang git; do
  if ! command -v "${dep}" >/dev/null 2>&1; then
    echo "error: missing required dependency '${dep}' in guest image" >&2
    exit 1
  fi
done
uv venv --system-site-packages --allow-existing
uv sync
BPFTOOL_BIN="$(command -v bpftool || true)"
if [[ -z "${BPFTOOL_BIN}" ]]; then
  BPFTOOL_BIN="$(find /usr/lib/linux-tools -type f -name bpftool 2>/dev/null | head -1 || true)"
fi
if [[ -z "${BPFTOOL_BIN}" ]]; then
  echo "error: bpftool not found after installing linux-tools packages" >&2
  exit 1
fi
"${BPFTOOL_BIN}" btf dump file /sys/kernel/btf/vmlinux format c > src/vmlinux.h
make -C src/reflex/implementations/ebpf BPFTOOL="${BPFTOOL_BIN}"

UNIXBENCH_DIR="${HOME}/byte-unixbench"
if [[ ! -x "${UNIXBENCH_DIR}/UnixBench/Run" ]]; then
  rm -rf "${UNIXBENCH_DIR}"
  git clone --depth 1 "${UNIXBENCH_URL}" "${UNIXBENCH_DIR}"
fi

UNIXBENCH="${UNIXBENCH_DIR}/UnixBench/Run" MODES="${MODES}" REFLEX_RESET_BETWEEN_BENCH=1 bash benchmarks/unixbench_compare.sh
GUEST

echo "[unixbench-qemu] complete"
echo "  results: ${REPO_ROOT}/data/runs/"
