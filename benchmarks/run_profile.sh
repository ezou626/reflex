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
  line="$(awk "/^[[:space:]]*${profile}:/,/^[[:space:]]*[A-Za-z0-9_]+:/{if(\$1==\"command:\"){sub(/^command:[[:space:]]*/,\"\"); print; exit}}" "${PROFILES}")"
  if [[ -z "${line}" ]]; then
    echo "error: could not find profile command for ${profile} in ${PROFILES}" >&2
    exit 1
  fi
  echo "${line}"
}

WORKLOAD_CMD="$(lookup_profile_cmd "${PROFILE}")"
echo "[benchmark] profile=${PROFILE} mode=${MODE} run_id=${RUN_ID}"
echo "[benchmark] workload=${WORKLOAD_CMD}"

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

set +e
sudo uv run python "${DAEMON_COMMON[@]}" >"${RUN_DIR}/daemon.log" 2>&1 &
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
