#!/usr/bin/env bash
# Build a reusable UnixBench QEMU image with dependencies preinstalled.
#
# Usage:
#   bash benchmarks/bake_unixbench_image.sh
#
# Useful env:
#   REFLEX_VM_CACHE        cache directory (default: ./data/qemu)
#   REFLEX_VM_SSH_PORT     host TCP port forwarded to guest :22 (default: 52222)
#   REFLEX_BAKE_SOURCE_IMG source cloud image name (default: noble-server-cloudimg-amd64.img)
#   REFLEX_BAKE_SOURCE_URL source cloud image URL
#   REFLEX_BAKE_OUTPUT_IMG output prebaked image name (default: noble-unixbench-deps-amd64.img)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="${SCRIPT_DIR%/benchmarks}"
cd "${REPO_ROOT}"

CACHE_DIR="${REFLEX_VM_CACHE:-${REPO_ROOT}/data/qemu}"
SSH_PORT="${REFLEX_VM_SSH_PORT:-52222}"
SOURCE_NAME="${REFLEX_BAKE_SOURCE_IMG:-noble-server-cloudimg-amd64.img}"
SOURCE_URL="${REFLEX_BAKE_SOURCE_URL:-https://cloud-images.ubuntu.com/noble/current/noble-server-cloudimg-amd64.img}"
OUTPUT_NAME="${REFLEX_BAKE_OUTPUT_IMG:-noble-unixbench-deps-amd64.img}"

BASE_IMG="${CACHE_DIR}/${SOURCE_NAME}"
OUTPUT_IMG="${CACHE_DIR}/${OUTPUT_NAME}"
OVERLAY="${CACHE_DIR}/bake-overlay-$$.qcow2"
SEED_ISO="${CACHE_DIR}/bake-seed-$$.img"
KEY="${CACHE_DIR}/id_ed25519"
PUB="${KEY}.pub"
KNOWN_HOSTS="${CACHE_DIR}/known_hosts.bake.$$"
CONSOLE_LOG="${CACHE_DIR}/bake-console-$$.log"
QEMU_PID_FILE="${CACHE_DIR}/bake-qemu-$$.pid"

step() { echo "[bake-unixbench-image] $*"; }
need_cmd() { command -v "$1" >/dev/null 2>&1 || { echo "error: missing '$1'" >&2; exit 1; }; }

cleanup() {
  if [[ -n "${QEMU_PID:-}" ]] && kill -0 "${QEMU_PID}" 2>/dev/null; then
    kill "${QEMU_PID}" 2>/dev/null || true
    wait "${QEMU_PID}" 2>/dev/null || true
  fi
  rm -f "${OVERLAY}" "${SEED_ISO}" "${KNOWN_HOSTS}" "${QEMU_PID_FILE}" 2>/dev/null || true
}
trap cleanup EXIT

need_cmd qemu-system-x86_64
need_cmd qemu-img
need_cmd ssh
need_cmd ssh-keygen
need_cmd cloud-localds

mkdir -p "${CACHE_DIR}"

if [[ ! -f "${KEY}" ]]; then
  step "Generating SSH key: ${KEY}"
  ssh-keygen -t ed25519 -f "${KEY}" -N "" -q
fi
chmod 600 "${KEY}" 2>/dev/null || true

if [[ ! -f "${BASE_IMG}" ]]; then
  step "Downloading source image ${SOURCE_URL}"
  if command -v curl >/dev/null 2>&1; then
    curl -fL --retry 3 -o "${BASE_IMG}.part" "${SOURCE_URL}"
  else
    wget -O "${BASE_IMG}.part" "${SOURCE_URL}"
  fi
  mv "${BASE_IMG}.part" "${BASE_IMG}"
fi

META_DATA="$(mktemp)"
USER_DATA="$(mktemp)"
INSTANCE_ID="reflex-bake-$(date +%s)-$$"
cat >"${META_DATA}" <<EOF
instance-id: ${INSTANCE_ID}
local-hostname: reflex-bake
EOF
PUB_LINE="$(cat "${PUB}")"
cat >"${USER_DATA}" <<EOF
#cloud-config
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

step "Starting QEMU"
qemu-system-x86_64 \
  -machine type=q35,accel=kvm \
  -cpu host \
  -smp 4 \
  -m 4096 \
  -display none \
  -daemonize \
  -pidfile "${QEMU_PID_FILE}" \
  -serial file:"${CONSOLE_LOG}" \
  -drive file="${OVERLAY}",if=virtio,cache=writeback \
  -drive file="${SEED_ISO}",if=virtio,format=raw \
  -netdev user,id=net0,hostfwd=tcp:127.0.0.1:${SSH_PORT}-:22 \
  -device virtio-net-pci,netdev=net0

QEMU_PID="$(cat "${QEMU_PID_FILE}")"
touch "${KNOWN_HOSTS}"
chmod 600 "${KNOWN_HOSTS}" 2>/dev/null || true

step "Waiting for SSH"
READY=0
for _ in $(seq 1 120); do
  OUT="$(ssh -vv -i "${KEY}" -p "${SSH_PORT}" \
    -o StrictHostKeyChecking=accept-new \
    -o UserKnownHostsFile="${KNOWN_HOSTS}" \
    -o ConnectTimeout=5 \
    -o BatchMode=yes \
    ubuntu@127.0.0.1 "echo ok" 2>&1 >/dev/null)" || true
  if ssh -i "${KEY}" -p "${SSH_PORT}" \
    -o StrictHostKeyChecking=accept-new \
    -o UserKnownHostsFile="${KNOWN_HOSTS}" \
    -o ConnectTimeout=5 \
    -o BatchMode=yes \
    ubuntu@127.0.0.1 "echo ok" >/dev/null 2>&1; then
    READY=1
    break
  fi
  if (( _ % 6 == 1 )); then
    echo "SSH attempt $_ failed:"
    printf '%s\n' "${OUT}" | tail -n 12
  fi
  sleep 5
done
if [[ "${READY}" -ne 1 ]]; then
  echo "error: SSH did not become ready" >&2
  exit 1
fi

step "Installing toolchain in guest"
ssh -i "${KEY}" -p "${SSH_PORT}" \
  -o StrictHostKeyChecking=yes \
  -o UserKnownHostsFile="${KNOWN_HOSTS}" \
  ubuntu@127.0.0.1 "bash -s" <<'GUEST'
set -euo pipefail
if command -v cloud-init >/dev/null 2>&1; then
  sudo cloud-init status --wait 2>/dev/null || true
fi
sudo apt-get update -qq
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
  build-essential clang libbpf-dev bpfcc-tools python3-bpfcc \
  git make perl curl ca-certificates unzip linux-tools-common >/dev/null
if ! sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
  "linux-headers-$(uname -r)" "linux-tools-$(uname -r)" >/dev/null; then
  sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
    linux-headers-generic linux-tools-generic >/dev/null
fi
if ! command -v uv >/dev/null 2>&1; then
  curl -LsSf https://astral.sh/uv/install.sh | sh
fi
echo "unixbench-baked" | sudo tee /etc/reflex-unixbench-image >/dev/null
sudo apt-get clean
sudo sync
GUEST

step "Stopping VM"
kill "${QEMU_PID}" 2>/dev/null || true
wait "${QEMU_PID}" 2>/dev/null || true
QEMU_PID=""

step "Writing baked image ${OUTPUT_IMG}"
rm -f "${OUTPUT_IMG}"
qemu-img convert -O qcow2 "${OVERLAY}" "${OUTPUT_IMG}"
step "Done"
