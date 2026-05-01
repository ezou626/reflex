#!/usr/bin/env bash
# Start a Reflex daemon inside an Ubuntu guest booted by QEMU.
#
# This is the Linux/macOS analogue of scripts/run_in_qemu.ps1. It creates a
# NoCloud seed ISO, boots QEMU, copies this repo into the guest over SSH, builds
# the eBPF loader, and starts the selected daemon.
#
# Usage:
#   bash scripts/run_in_qemu.sh --daemon heuristic
#
# Useful env:
#   REFLEX_VM_CACHE       cache directory (default: ./data/qemu)
#   REFLEX_VM_PORT        host TCP port forwarded to guest :22 (default: 52222)
#   REFLEX_VM_DISK_GB     guest root disk size in GiB (default: 24)
#   REFLEX_VM_MEMORY_MB   guest memory MiB (default: 4096)
#   REFLEX_VM_CPUS        guest vCPUs (default: 6)
#   REFLEX_QEMU_ACCEL     override accelerator (kvm, hvf, tcg)
#   OPENAI_API_KEY        passed through to OpenAI controller runs
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${REPO_ROOT}"

DAEMON=""
PORT="${REFLEX_VM_PORT:-52222}"
DISK_GB="${REFLEX_VM_DISK_GB:-24}"
MEMORY_MB="${REFLEX_VM_MEMORY_MB:-4096}"
CPUS="${REFLEX_VM_CPUS:-6}"
CACHE_DIR="${REFLEX_VM_CACHE:-${REPO_ROOT}/data/qemu}"
OPENAI_API_KEY_VALUE="${OPENAI_API_KEY:-}"
UBUNTU_IMAGE_URL="https://cloud-images.ubuntu.com/noble/current/noble-server-cloudimg-amd64.img"
FULL=0
DRY_RUN=0

usage() {
  cat <<'EOF'
Usage: bash scripts/run_in_qemu.sh --daemon heuristic [options]

Options:
  --daemon NAME           Required daemon name, e.g. heuristic.
  --port PORT             Host SSH forward port. Default: 52222.
  --disk-gb GB            Guest disk size. Default: 24.
  --memory-mb MB          Guest memory. Default: 4096.
  --cpus N                Guest vCPUs. Default: 6.
  --full                  Accepted for parity with the PowerShell wrapper.
  --dry-run               Start the daemon in dry-run mode.
  -h, --help              Show this help.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --daemon|-Daemon) DAEMON="$2"; shift ;;
    --daemon=*|-Daemon=*) DAEMON="${1#*=}" ;;
    --port|-Port) PORT="$2"; shift ;;
    --port=*|-Port=*) PORT="${1#*=}" ;;
    --disk-gb) DISK_GB="$2"; shift ;;
    --disk-gb=*) DISK_GB="${1#--disk-gb=}" ;;
    -DiskGB) DISK_GB="$2"; shift ;;
    --memory-mb) MEMORY_MB="$2"; shift ;;
    --memory-mb=*) MEMORY_MB="${1#--memory-mb=}" ;;
    -MemoryMB) MEMORY_MB="$2"; shift ;;
    --cpus) CPUS="$2"; shift ;;
    --cpus=*) CPUS="${1#--cpus=}" ;;
    -Cpus) CPUS="$2"; shift ;;
    --full|-Full) FULL=1 ;;
    --dry-run|-DryRun) DRY_RUN=1 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown option: $1" >&2; usage >&2; exit 1 ;;
  esac
  shift
done

if [[ -z "${DAEMON}" ]]; then
  echo "error: --daemon is required, e.g. --daemon heuristic" >&2
  exit 1
fi
if [[ ! "${DAEMON}" =~ ^[A-Za-z0-9_.-]+$ ]]; then
  echo "error: daemon name must contain only letters, numbers, '.', '_', or '-'" >&2
  exit 1
fi

step() {
  echo "[run-in-qemu] $*"
}

need_cmd() {
  command -v "$1" >/dev/null 2>&1 || {
    echo "error: missing '$1'. Install it and retry." >&2
    exit 1
  }
}

abs_path() {
  local path="$1"
  if command -v realpath >/dev/null 2>&1; then
    realpath "$path"
  else
    (cd "$(dirname "$path")" && printf '%s/%s\n' "$(pwd -P)" "$(basename "$path")")
  fi
}

read_dotenv_key() {
  local file="$1"
  local name="$2"
  [[ -f "$file" ]] || return 0
  awk -F= -v key="$name" '
    /^[[:space:]]*#/ || /^[[:space:]]*$/ { next }
    {
      line=$0
      sub(/^[[:space:]]*export[[:space:]]+/, "", line)
      split(line, parts, "=")
      k=parts[1]
      gsub(/^[[:space:]]+|[[:space:]]+$/, "", k)
      if (k != key) next
      v=substr(line, index(line, "=") + 1)
      gsub(/^[[:space:]]+|[[:space:]]+$/, "", v)
      if ((substr(v,1,1) == "\"" && substr(v,length(v),1) == "\"") ||
          (substr(v,1,1) == "'"'"'" && substr(v,length(v),1) == "'"'"'")) {
        v=substr(v,2,length(v)-2)
      }
      print v
      exit
    }
  ' "$file"
}

make_seed_iso() {
  local seed_dir="$1"
  local seed_iso="$2"
  if command -v cloud-localds >/dev/null 2>&1; then
    cloud-localds "$seed_iso" "$seed_dir/user-data" "$seed_dir/meta-data"
  elif command -v xorriso >/dev/null 2>&1; then
    xorriso -as mkisofs -output "$seed_iso" -volid cidata -joliet -rock "$seed_dir" >/dev/null
  elif command -v genisoimage >/dev/null 2>&1; then
    genisoimage -output "$seed_iso" -volid cidata -joliet -rock "$seed_dir" >/dev/null
  elif command -v mkisofs >/dev/null 2>&1; then
    mkisofs -output "$seed_iso" -volid cidata -joliet -rock "$seed_dir" >/dev/null
  elif [[ "$(uname -s)" == "Darwin" ]] && command -v hdiutil >/dev/null 2>&1; then
    hdiutil makehybrid -iso -joliet -default-volume-name cidata -o "$seed_iso" "$seed_dir" >/dev/null
  else
    cat >&2 <<'EOF'
error: no NoCloud ISO tool found.
Install one of: cloud-localds, xorriso, genisoimage, mkisofs.
On macOS, hdiutil is used automatically if available.
EOF
    exit 1
  fi
}

choose_accel() {
  if [[ -n "${REFLEX_QEMU_ACCEL:-}" ]]; then
    printf '%s\n' "${REFLEX_QEMU_ACCEL}"
    return
  fi
  case "$(uname -s)" in
    Linux)
      if [[ -r /dev/kvm ]]; then
        printf 'kvm\n'
      else
        printf 'tcg\n'
      fi
      ;;
    Darwin)
      if [[ "$(uname -m)" == "x86_64" ]]; then
        printf 'hvf\n'
      else
        printf 'tcg\n'
      fi
      ;;
    *)
      printf 'tcg\n'
      ;;
  esac
}

wait_for_ssh() {
  local key="$1"
  local port="$2"
  local known_hosts="$3"
  local ready=0
  for i in $(seq 1 120); do
    if ssh -i "$key" -p "$port" \
      -o StrictHostKeyChecking=accept-new \
      -o UserKnownHostsFile="$known_hosts" \
      -o ConnectTimeout=5 \
      -o BatchMode=yes \
      ubuntu@127.0.0.1 "echo ok" >/dev/null 2>&1; then
      ready=1
      break
    fi
    if (( i % 6 == 1 )); then
      step "SSH not ready yet (attempt ${i}/120)"
    fi
    sleep 5
  done
  [[ "$ready" == "1" ]]
}

ssh_guest() {
  ssh -i "$KEY" -p "$PORT" \
    -o StrictHostKeyChecking=yes \
    -o UserKnownHostsFile="$KNOWN_HOSTS" \
    -o ConnectTimeout=30 \
    ubuntu@127.0.0.1 "$@"
}

cleanup() {
  if [[ -n "${QEMU_PID:-}" ]] && kill -0 "$QEMU_PID" 2>/dev/null; then
    step "Stopping QEMU"
    kill "$QEMU_PID" 2>/dev/null || true
    wait "$QEMU_PID" 2>/dev/null || true
  fi
  rm -f "$OVERLAY" "$SEED_ISO" "$REPO_ZIP" "$KNOWN_HOSTS" "$RUN_ROOT_FILE" "${GUEST_SCRIPT:-}" 2>/dev/null || true
  rm -rf "$SEED_DIR" 2>/dev/null || true
}
trap cleanup EXIT

need_cmd qemu-system-x86_64
need_cmd qemu-img
need_cmd ssh
need_cmd scp
need_cmd ssh-keygen
need_cmd zip

if [[ -z "$OPENAI_API_KEY_VALUE" ]]; then
  OPENAI_API_KEY_VALUE="$(read_dotenv_key "${REPO_ROOT}/.env" OPENAI_API_KEY || true)"
  if [[ -n "$OPENAI_API_KEY_VALUE" ]]; then
    step "Loaded OPENAI_API_KEY from .env"
  fi
fi

mkdir -p "$CACHE_DIR" "$REPO_ROOT/data"

BASE_IMG="${CACHE_DIR}/noble-server-cloudimg-amd64.img"
OVERLAY="${CACHE_DIR}/reflex-overlay-$$.qcow2"
SEED_ISO="${CACHE_DIR}/reflex-seed-$$.iso"
SEED_DIR="${CACHE_DIR}/seed-$$"
CONSOLE_LOG="${CACHE_DIR}/reflex-console-$$.log"
QEMU_LOG="${CACHE_DIR}/reflex-qemu-$$.log"
KEY="${CACHE_DIR}/id_ed25519"
PUB="${KEY}.pub"
KNOWN_HOSTS="${CACHE_DIR}/known_hosts.reflex.$$"
REPO_ZIP="${CACHE_DIR}/reflex-$$.zip"
RUN_ROOT_FILE="${CACHE_DIR}/daemon-started-$$.txt"
GUEST_SCRIPT="${CACHE_DIR}/run-reflex-daemon-$$.sh"
QEMU_PID=""

if [[ ! -f "$KEY" ]]; then
  step "Generating SSH key: $KEY"
  ssh-keygen -t ed25519 -f "$KEY" -N "" -q
fi
chmod 600 "$KEY" 2>/dev/null || true

if [[ ! -f "$BASE_IMG" ]]; then
  step "Downloading Ubuntu cloud image"
  if command -v curl >/dev/null 2>&1; then
    curl -fL --retry 3 -o "${BASE_IMG}.part" "$UBUNTU_IMAGE_URL"
  elif command -v wget >/dev/null 2>&1; then
    wget -O "${BASE_IMG}.part" "$UBUNTU_IMAGE_URL"
  else
    echo "error: missing curl or wget for image download" >&2
    exit 1
  fi
  mv "${BASE_IMG}.part" "$BASE_IMG"
fi

step "Creating cloud-init seed ISO"
rm -rf "$SEED_DIR"
mkdir -p "$SEED_DIR"
PUB_LINE="$(tr -d '\r\n' < "$PUB")"
cat >"$SEED_DIR/meta-data" <<EOF
instance-id: iid-reflex-$$
local-hostname: reflex-qemu
EOF
cat >"$SEED_DIR/user-data" <<EOF
#cloud-config
users:
  - default

ssh_authorized_keys:
  - ${PUB_LINE}
EOF
make_seed_iso "$SEED_DIR" "$SEED_ISO"

step "Creating VM overlay"
qemu-img create -f qcow2 -F qcow2 -b "$(abs_path "$BASE_IMG")" "$OVERLAY"
qemu-img resize "$OVERLAY" "${DISK_GB}G"

ACCEL="$(choose_accel)"
CPU_MODEL="qemu64"
if [[ "$ACCEL" == "kvm" ]]; then
  CPU_MODEL="host"
elif [[ "$ACCEL" == "tcg" ]]; then
  step "Using QEMU TCG emulation; this will be much slower than KVM/HVF."
fi

step "Starting QEMU on 127.0.0.1:${PORT} with accel=${ACCEL}"
qemu-system-x86_64 \
  -machine "type=q35,accel=${ACCEL}" \
  -smbios "type=1,serial=ds=nocloud" \
  -cpu "$CPU_MODEL" \
  -smp "$CPUS" \
  -m "$MEMORY_MB" \
  -display none \
  -serial "file:${CONSOLE_LOG}" \
  -drive "file=${OVERLAY},if=virtio,cache=writeback" \
  -cdrom "$SEED_ISO" \
  -netdev "user,id=net0,hostfwd=tcp:127.0.0.1:${PORT}-:22" \
  -device e1000,netdev=net0 \
  >"$QEMU_LOG" 2>&1 &
QEMU_PID="$!"

touch "$KNOWN_HOSTS"
chmod 600 "$KNOWN_HOSTS" 2>/dev/null || true

step "Waiting for SSH"
if ! wait_for_ssh "$KEY" "$PORT" "$KNOWN_HOSTS"; then
  echo "error: SSH did not become ready. Last console lines:" >&2
  tail -50 "$CONSOLE_LOG" >&2 || true
  echo "Last QEMU log lines:" >&2
  tail -50 "$QEMU_LOG" >&2 || true
  exit 1
fi
step "SSH ready"

step "Preparing repo archive"
rm -f "$REPO_ZIP"
zip -r -q "$REPO_ZIP" . \
  --exclude './.git/*' \
  --exclude './.venv/*' \
  --exclude './.testvenv/*' \
  --exclude './.uv-cache/*' \
  --exclude './.pytest_cache/*' \
  --exclude './.ruff_cache/*' \
  --exclude './data/qemu-windows/*' \
  --exclude './data/qemu/*' \
  --exclude './__pycache__/*' \
  --exclude './.worktrees/*'

step "Copying repo archive to guest"
scp -i "$KEY" -P "$PORT" \
  -o StrictHostKeyChecking=yes \
  -o UserKnownHostsFile="$KNOWN_HOSTS" \
  "$REPO_ZIP" ubuntu@127.0.0.1:/home/ubuntu/reflex.zip

OPENAI_EXPORT_LINE=""
if [[ -n "$OPENAI_API_KEY_VALUE" ]]; then
  KEY_B64="$(printf '%s' "$OPENAI_API_KEY_VALUE" | base64 | tr -d '\n')"
  OPENAI_EXPORT_LINE="export OPENAI_API_KEY=\$(printf '%s' '$KEY_B64' | base64 -d)"
else
  step "OPENAI_API_KEY not provided; OpenAI controller runs will no-op."
fi

DRY_RUN_ARG=""
if [[ "$DRY_RUN" == "1" ]]; then
  DRY_RUN_ARG="--dry-run"
fi

cat >"$GUEST_SCRIPT" <<EOF
set -euo pipefail
export PATH="\$HOME/.local/bin:\$PATH"
${OPENAI_EXPORT_LINE}

wait_for_apt() {
  if command -v cloud-init >/dev/null 2>&1; then
    sudo cloud-init status --wait 2>/dev/null || true
  fi
  local n=0
  while sudo fuser /var/lib/apt/lists/lock /var/lib/dpkg/lock-frontend /var/lib/dpkg/lock >/dev/null 2>&1; do
    n=\$((n + 1))
    if [[ "\$n" -gt 90 ]]; then
      echo "error: apt/dpkg locks still held" >&2
      exit 1
    fi
    sleep 2
  done
}

wait_for_apt
sudo apt-get update -qq
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \\
  build-essential clang libbpf-dev bpfcc-tools python3-bpfcc \\
  git make perl curl ca-certificates unzip linux-tools-common >/dev/null
if ! sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \\
  "linux-headers-\$(uname -r)" "linux-tools-\$(uname -r)" >/dev/null; then
  sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \\
    linux-headers-generic linux-tools-generic >/dev/null
fi
sudo apt-get clean

if ! command -v uv >/dev/null 2>&1; then
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="\$HOME/.local/bin:\$PATH"
fi

rm -rf /home/ubuntu/reflex
mkdir -p /home/ubuntu/reflex
unzip -q -o /home/ubuntu/reflex.zip -d /home/ubuntu/reflex
find /home/ubuntu/reflex -name "*.sh" -exec sed -i 's/\\r//' {} +
cd /home/ubuntu/reflex
uv venv --system-site-packages --allow-existing
uv sync --extra openai

BPFTOOL_BIN="\$(command -v bpftool || true)"
if [[ -z "\$BPFTOOL_BIN" ]]; then
  BPFTOOL_BIN="\$(find /usr/lib/linux-tools -type f -name bpftool 2>/dev/null | head -1 || true)"
fi
if [[ -z "\$BPFTOOL_BIN" ]]; then
  echo "error: bpftool not found" >&2
  exit 1
fi
"\$BPFTOOL_BIN" btf dump file /sys/kernel/btf/vmlinux format c > src/vmlinux.h
make -C src/reflex/implementations/ebpf BPFTOOL="\$BPFTOOL_BIN"

OPENAI_API_KEY="\${OPENAI_API_KEY:-}"
DAEMON_LOG="/home/ubuntu/reflex/daemon.log"
DAEMON_PIDFILE="/home/ubuntu/reflex/daemon.pid"
nohup sudo env OPENAI_API_KEY="\$OPENAI_API_KEY" uv run reflex --no-sudo ${DRY_RUN_ARG} ${DAEMON} > "\$DAEMON_LOG" 2>&1 &
echo \$! > "\$DAEMON_PIDFILE"
printf '%s\\n' "\$(date -u +%Y-%m-%dT%H:%M:%SZ)" "\$DAEMON_PIDFILE" > /home/ubuntu/reflex/daemon_started.txt
EOF

step "Running guest setup and starting daemon"
scp -i "$KEY" -P "$PORT" \
  -o StrictHostKeyChecking=yes \
  -o UserKnownHostsFile="$KNOWN_HOSTS" \
  "$GUEST_SCRIPT" ubuntu@127.0.0.1:/home/ubuntu/run_reflex_daemon.sh
ssh_guest "bash /home/ubuntu/run_reflex_daemon.sh"
rm -f "$GUEST_SCRIPT"

step "Copying daemon start marker back to host"
scp -i "$KEY" -P "$PORT" \
  -o StrictHostKeyChecking=yes \
  -o UserKnownHostsFile="$KNOWN_HOSTS" \
  ubuntu@127.0.0.1:/home/ubuntu/reflex/daemon_started.txt \
  "$RUN_ROOT_FILE"

MARKER="$(tr -d '\r' < "$RUN_ROOT_FILE")"
if [[ -z "$MARKER" ]]; then
  echo "error: daemon start marker was empty" >&2
  exit 1
fi

step "Daemon started on guest: ${MARKER}"
