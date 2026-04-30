#!/usr/bin/env bash
set -euo pipefail

# Determine repo root (one level up from this script)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="${SCRIPT_DIR%/scripts}"
cd "${REPO_ROOT}"

# Basic OS check for Ubuntu 24.04 (optional but helps reproducibility)
if [ -r /etc/os-release ]; then
  . /etc/os-release
  if [ "${ID:-}" != "ubuntu" ] || [ "${VERSION_ID:-}" != "24.04" ]; then
    echo "[reflex] Warning: Intended for Ubuntu 24.04, detected ${ID:-unknown} ${VERSION_ID:-unknown}." >&2
  fi
fi

# Ensure uv is available (user note: may already be installed)
export PATH="$HOME/.local/bin:$PATH"
if ! command -v uv >/dev/null 2>&1; then
  echo "[reflex] Installing uv (Python package manager)..."
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="$HOME/.local/bin:$PATH"
else
  echo "[reflex] uv already installed; skipping installer."
fi

echo "[reflex] Installing system dependencies via apt..."

sudo apt-get update -y
# Base packages common to all environments
COMMON_PKGS=(
  auditd
  build-essential
  clang
  llvm
  libbpf-dev
  libelf-dev
  zlib1g-dev
  pkg-config
  bpfcc-tools
  python3-bpfcc
  libbpfcc-dev
  cloud-image-utils
  stress-ng
  fio
  sysbench
  ffmpeg
  blender
  qemu-system-x86
)

# Kernel-specific tooling is best effort. Some hosts, especially Debian hosts
# running cloud/custom kernels, do not expose linux-headers-$(uname -r) through
# the configured apt repositories. That should not block QEMU guest benchmarks.
UNAME_REL="$(uname -r)"

sudo DEBIAN_FRONTEND=noninteractive apt-get install -y "${COMMON_PKGS[@]}"

if printf '%s\n' "$UNAME_REL" | grep -qi 'microsoft'; then
  echo "[reflex] Detected WSL kernel (${UNAME_REL}); trying generic kernel tools."
  sudo DEBIAN_FRONTEND=noninteractive apt-get install -y \
    linux-headers-generic linux-tools-common linux-tools-generic || true
else
  if ! sudo DEBIAN_FRONTEND=noninteractive apt-get install -y \
    linux-headers-"${UNAME_REL}" linux-tools-common linux-tools-"${UNAME_REL}"; then
    echo "[reflex] Warning: matching kernel headers/tools unavailable for ${UNAME_REL}." >&2
    echo "[reflex] Continuing; QEMU benchmarks install guest kernel tooling inside the VM." >&2
    sudo DEBIAN_FRONTEND=noninteractive apt-get install -y \
      linux-tools-common linux-tools-generic 2>/dev/null || true
  fi
fi

# bpftool and perf are provided by linux-tools-* packages for the running kernel
if ! command -v bpftool >/dev/null 2>&1; then
  echo "[reflex] Warning: bpftool not found on PATH after install; check linux-tools packages manually." >&2
fi
if ! command -v perf >/dev/null 2>&1; then
  echo "[reflex] Warning: perf not found on PATH after install; check linux-tools packages manually." >&2
fi

echo "[reflex] Syncing Python environment with uv (using uv.lock if present)..."
# BCC comes from the distro (python3-bpfcc); the venv needs system site packages to import it.
uv venv --system-site-packages --allow-existing
uv sync

echo
echo "[reflex] Setup complete. Next steps:"
echo "  - Ensure \"$HOME/.local/bin\" is in your PATH for uv commands."
echo "  - Initialize optional reference submodule (KernMLOps):"
echo "      git submodule update --init external/KernMLOps"
echo "  - Start the QEMU VM:"
echo "      bash scripts/run_in_qemu.sh --modes heuristic"
