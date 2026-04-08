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
  build-essential
  clang
  libbpf-dev
  bpfcc-tools
  python3-bpfcc
  stress-ng
  fio
  qemu-system-x86
)

# Kernel-specific tooling
UNAME_REL="$(uname -r)"
KERNEL_PKGS=()

if printf '%s\n' "$UNAME_REL" | grep -qi 'microsoft'; then
  # WSL2 kernels typically don't have versioned linux-headers/linux-tools
  echo "[reflex] Detected WSL kernel (${UNAME_REL}); using generic linux-headers/linux-tools packages."
  KERNEL_PKGS+=(linux-headers-generic linux-tools-common linux-tools-generic)
else
  # Stock Ubuntu kernels should have matching versioned packages
  KERNEL_PKGS+=(linux-headers-"${UNAME_REL}" linux-tools-common linux-tools-"${UNAME_REL}")
fi

sudo DEBIAN_FRONTEND=noninteractive apt-get install -y "${COMMON_PKGS[@]}" "${KERNEL_PKGS[@]}"

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
echo "  - Run the MVP ring-buffer collector (needs root for eBPF load):"
echo "      sudo uv run python daemon/main.py"
