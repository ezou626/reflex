#!/usr/bin/env bash
# Boot an Ubuntu 24.04 cloud image under QEMU (KVM when available), install BCC
# inside the guest, 9p-mount this repo, and smoke-test daemon/main.py.
#
# Requirements (host): qemu-system-x86, cloud-image-utils (cloud-localds),
# openssh-client, curl or wget, optional: genisoimage (often pulled with cloud-image-utils).
#
# WSL2: /dev/kvm is often present, but your user must be in group "kvm" OR run:
#   sudo bash scripts/test_mvp_qemu.sh
#
# Env:
#   REFLEX_VM_CACHE    - cache directory (default: ~/.cache/reflex-qemu, or $HOME under sudo)
#   REFLEX_VM_SSH_PORT - host TCP port forwarded to guest :22 (default: 52222)
#   REFLEX_VM_DISK_GB  - guest root disk size in GiB (default: 20; BCC+headers+LLVM needs space)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="${SCRIPT_DIR%/scripts}"
cd "${REPO_ROOT}"

# With `sudo bash`, $HOME is often /root — cache and keys live there unless you
# set REFLEX_VM_CACHE to e.g. /home/you/.cache/reflex-qemu.
CACHE_DIR="${REFLEX_VM_CACHE:-${HOME}/.cache/reflex-qemu}"
SSH_PORT="${REFLEX_VM_SSH_PORT:-52222}"
DISK_GB="${REFLEX_VM_DISK_GB:-20}"
BASE_NAME="noble-server-cloudimg-amd64.img"
BASE_URL="https://cloud-images.ubuntu.com/noble/current/${BASE_NAME}"
BASE_IMG="${CACHE_DIR}/${BASE_NAME}"
OVERLAY="${CACHE_DIR}/overlay-$$.qcow2"
SEED_ISO="${CACHE_DIR}/seed-$$.img"
KEY="${CACHE_DIR}/id_ed25519"
PUB="${KEY}.pub"
CONSOLE_LOG="${CACHE_DIR}/console-$$.log"
QEMU_PID_FILE="${CACHE_DIR}/qemu-$$.pid"
# Ephemeral VMs get new host keys every boot; a shared known_hosts makes
# StrictHostKeyChecking=accept-new fail with "REMOTE HOST IDENTIFICATION HAS CHANGED".
KNOWN_HOSTS="${CACHE_DIR}/known_hosts.$$"

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

if [[ ! -r /dev/kvm ]]; then
  echo "error: cannot read /dev/kvm (KVM device)." >&2
  echo "  - Add your user to the kvm group, then re-login to WSL:" >&2
  echo "      sudo usermod -aG kvm \"${SUDO_USER:-$USER}\"" >&2
  echo "  - Or run this whole script under sudo (uses KVM as root):" >&2
  echo "      sudo bash scripts/test_mvp_qemu.sh" >&2
  exit 1
fi

if ! command -v cloud-localds >/dev/null 2>&1; then
  echo "error: cloud-localds not found. Install: sudo apt install cloud-image-utils" >&2
  exit 1
fi

mkdir -p "${CACHE_DIR}"
# Leftover from older script versions; causes host key mismatch on next run.
rm -f "${CACHE_DIR}/known_hosts"

if [[ ! -f "${KEY}" ]]; then
  echo "[reflex-vm] generating SSH key for guest access: ${KEY}"
  ssh-keygen -t ed25519 -f "${KEY}" -N "" -q
fi

if [[ ! -f "${BASE_IMG}" ]]; then
  echo "[reflex-vm] downloading ${BASE_URL} (one-time, ~600MiB)..."
  mkdir -p "${CACHE_DIR}"
  if command -v curl >/dev/null 2>&1; then
    curl -fL --retry 3 -o "${BASE_IMG}.part" "${BASE_URL}"
  else
    wget -O "${BASE_IMG}.part" "${BASE_URL}"
  fi
  mv "${BASE_IMG}.part" "${BASE_IMG}"
fi

META_DATA="$(mktemp)"
USER_DATA="$(mktemp)"

INSTANCE_ID="reflex-$(date +%s)-$$"
cat >"${META_DATA}" <<EOF
instance-id: ${INSTANCE_ID}
local-hostname: reflex-mvp
EOF

PUB_LINE="$(cat "${PUB}")"
cat >"${USER_DATA}" <<EOF
#cloud-config
# Do not run apt from cloud-init — our SSH smoke test installs packages and would
# race cloud-init's package_update (apt lock held by process ...).
package_update: false
growpart:
  mode: auto
  devices: ["/"]
  ignore_growroot_disabled: false
resize_rootfs: true
ssh_authorized_keys:
  - ${PUB_LINE}
EOF

cloud-localds "${SEED_ISO}" "${USER_DATA}" "${META_DATA}"
rm -f "${META_DATA}" "${USER_DATA}"

qemu-img create -f qcow2 -F qcow2 -b "$(realpath "${BASE_IMG}")" "${OVERLAY}"
# Base cloud image is ~3–4 GiB; BCC + kernel headers + clang easily exceeds that.
qemu-img resize "${OVERLAY}" "${DISK_GB}G"
echo "[reflex-vm] guest disk (overlay) sized to ${DISK_GB} GiB virtual capacity"

echo "[reflex-vm] starting QEMU (KVM) on 127.0.0.1:${SSH_PORT} -> guest :22 ..."
echo "[reflex-vm] console log: ${CONSOLE_LOG}"

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

rm -f "${KNOWN_HOSTS}"
touch "${KNOWN_HOSTS}"
chmod 600 "${KNOWN_HOSTS}" 2>/dev/null || true

echo "[reflex-vm] waiting for SSH (first boot can take several minutes)..."
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
  echo "error: SSH did not become ready in time. Last console lines:" >&2
  tail -50 "${CONSOLE_LOG}" >&2 || true
  exit 1
fi

echo "[reflex-vm] guest is up; installing BCC and running MVP daemon smoke test..."

ssh \
  -i "${KEY}" \
  -p "${SSH_PORT}" \
  -o StrictHostKeyChecking=yes \
  -o UserKnownHostsFile="${KNOWN_HOSTS}" \
  -o ConnectTimeout=30 \
  ubuntu@127.0.0.1 \
  bash -s <<'GUEST'
set -euo pipefail

wait_for_apt() {
  if command -v cloud-init >/dev/null 2>&1; then
    sudo cloud-init status --wait 2>/dev/null || true
  fi
  if ! command -v fuser >/dev/null 2>&1; then
    echo "[guest] fuser not found; sleeping 20s for apt/cloud-init" >&2
    sleep 20
    return 0
  fi
  local n=0
  local max=90
  while true; do
    if ! sudo fuser /var/lib/apt/lists/lock >/dev/null 2>&1 \
      && ! sudo fuser /var/lib/dpkg/lock-frontend >/dev/null 2>&1 \
      && ! sudo fuser /var/lib/dpkg/lock >/dev/null 2>&1; then
      return 0
    fi
    n=$((n + 1))
    if [[ "${n}" -ge "${max}" ]]; then
      echo "error: apt/dpkg locks still held after ~$((max * 2))s" >&2
      sudo fuser -v /var/lib/apt/lists/lock /var/lib/dpkg/lock 2>&1 || true
      exit 1
    fi
    sleep 2
  done
}

echo "[guest] waiting for cloud-init / apt locks..."
wait_for_apt

echo "[guest] disk before apt:" >&2
df -h / >&2

sudo apt-get update -qq
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
  python3-bpfcc bpfcc-tools "linux-headers-$(uname -r)" build-essential clang >/dev/null
sudo apt-get clean
echo "[guest] disk after apt:" >&2
df -h / >&2
sudo modprobe 9pnet_virtio 2>/dev/null || true
sudo mkdir -p /mnt/reflex
sudo mount -t 9p -o trans=virtio,version=9p2000.L hostshare /mnt/reflex
test -f /mnt/reflex/daemon/main.py
test -f /mnt/reflex/ebpf/mvp_ringbuf.bpf.c
sudo rm -f /tmp/mvp.jsonl
set +e
sudo python3 /mnt/reflex/daemon/main.py -o /tmp/mvp.jsonl &
DP=$!
sleep 3
for _ in $(seq 1 40); do /bin/true; done
sudo kill "${DP}" 2>/dev/null
wait "${DP}" 2>/dev/null
set -e
if [[ ! -s /tmp/mvp.jsonl ]]; then
  echo "error: /tmp/mvp.jsonl missing or empty (eBPF daemon produced no lines)." >&2
  exit 1
fi
echo "--- sample events ---"
head -5 /tmp/mvp.jsonl
echo "--- line count ---"
wc -l /tmp/mvp.jsonl
GUEST

echo "[reflex-vm] OK: MVP daemon wrote JSONL inside the guest."
