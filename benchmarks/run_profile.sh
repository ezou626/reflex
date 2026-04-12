#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="${SCRIPT_DIR%/benchmarks}"
cd "${REPO_ROOT}"

PROFILE="${1:-cpu_bound}"
MODE="${2:-baseline}" # baseline|tuned
RUN_ID="${3:-profile-${PROFILE}-${MODE}-$(date +%Y%m%d-%H%M%S)}"
RUN_DIR="${REPO_ROOT}/data/runs/${RUN_ID}"
POLICY_FILE="${REPO_ROOT}/configs/tuning_policy.yaml"
CATALOG="${REPO_ROOT}/configs/tuner_catalog.yaml"
PROFILES="${REPO_ROOT}/configs/profiles.yaml"

mkdir -p "${RUN_DIR}"

lookup_profile_cmd() {
  local profile="$1"
  local line
  line="$(
    awk -v p="${profile}" '
      BEGIN { in_profile = 0 }
      $0 ~ "^  " p ":" { in_profile = 1; next }
      in_profile && $0 ~ "^  [A-Za-z0-9_]+:" { in_profile = 0 }
      in_profile && $0 ~ "^    command:" {
        sub(/^    command:[[:space:]]*/, "", $0)
        print
        exit
      }
    ' "${PROFILES}"
  )"
  if [[ -z "${line}" ]]; then
    echo "error: could not find profile command for ${profile} in ${PROFILES}" >&2
    exit 1
  fi
  echo "${line}"
}

WORKLOAD_CMD="$(lookup_profile_cmd "${PROFILE}")"
echo "[benchmark] profile=${PROFILE} mode=${MODE} run_id=${RUN_ID}"
echo "[benchmark] workload=${WORKLOAD_CMD}"

UV_BIN="$(command -v uv || true)"
if [[ -n "${UV_BIN}" ]]; then
  DAEMON_LAUNCH=("${UV_BIN}" "run" "python")
else
  echo "[benchmark] warning: 'uv' not found in user PATH; falling back to system python3" >&2
  DAEMON_LAUNCH=("python3")
fi

DAEMON_COMMON=(
  "daemon/main.py"
  "--run-id" "${RUN_ID}"
  "--run-dir" "${RUN_DIR}"
  "--policy-file" "${POLICY_FILE}"
  "--tuner-catalog" "${CATALOG}"
)

if [[ "${MODE}" == "baseline" ]]; then
  DAEMON_COMMON+=("--dry-run")
fi

if ! sudo "${DAEMON_LAUNCH[@]}" -c "import bcc" >/dev/null 2>&1; then
  cat >&2 <<'EOF'
error: Python module 'bcc' not importable in the privileged runtime.
Fix:
  sudo apt install python3-bpfcc bpfcc-tools
  uv venv --system-site-packages --allow-existing
  uv sync
  (scripts/setup_dev_env.sh does this automatically.)
Then retry this benchmark command.
EOF
  exit 1
fi

set +e
sudo "${DAEMON_LAUNCH[@]}" "${DAEMON_COMMON[@]}" >"${RUN_DIR}/daemon.log" 2>&1 &
DPID=$!
sleep 3
bash -lc "${WORKLOAD_CMD}" >"${RUN_DIR}/workload.log" 2>&1
sleep 2
sudo kill "${DPID}" 2>/dev/null
wait "${DPID}" 2>/dev/null
set -e

echo "[benchmark] outputs:"
echo "  ${RUN_DIR}/summary.jsonl"
echo "  ${RUN_DIR}/decisions.jsonl"
echo "  ${RUN_DIR}/workload.log"
