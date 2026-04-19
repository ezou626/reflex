#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="${SCRIPT_DIR%/benchmarks}"
cd "${REPO_ROOT}"

PROFILE="${1:-cpu_bound}"
MODE="${2:-noop}" # heuristic|noop|workload_only (legacy: tuned|baseline)
RUN_ID="${3:-profile-${PROFILE}-${MODE}-$(date +%Y%m%d-%H%M%S)}"
RUN_DIR="${REPO_ROOT}/data/runs/${RUN_ID}"
POLICY_FILE="${REPO_ROOT}/configs/tuning_policy.yaml"
CATALOG="${REPO_ROOT}/configs/tuner_catalog.yaml"
PROFILES="${REPO_ROOT}/configs/profiles.yaml"
SAMPLER="${REPO_ROOT}/benchmarks/sample_host_metrics.py"

IDLE_BEFORE_SEC="${IDLE_BEFORE_SEC:-3}"
IDLE_AFTER_SEC="${IDLE_AFTER_SEC:-2}"

mkdir -p "${RUN_DIR}"

case "${MODE}" in
  tuned) MODE="heuristic" ;;
  baseline) MODE="noop" ;;
esac

if [[ "${MODE}" != "heuristic" && "${MODE}" != "noop" && "${MODE}" != "workload_only" ]]; then
  echo "error: mode must be heuristic|noop|workload_only (or legacy tuned|baseline)" >&2
  exit 1
fi

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

lookup_profile_field() {
  local profile="$1"
  local field="$2"
  python3 -c "
import sys, yaml
from pathlib import Path
root = yaml.safe_load(Path(sys.argv[1]).read_text(encoding='utf-8'))
prof = root.get('profiles', {}).get(sys.argv[2], {})
v = prof.get(sys.argv[3], 0)
if isinstance(v, (int, float)):
    print(v)
else:
    print(0)
" "${PROFILES}" "${profile}" "${field}"
}

WORKLOAD_CMD="$(lookup_profile_cmd "${PROFILE}")"
WARMUP_SEC="$(lookup_profile_field "${PROFILE}" warmup_sec)"
DURATION_SEC="$(lookup_profile_field "${PROFILE}" duration_sec)"

echo "[benchmark] profile=${PROFILE} mode=${MODE} run_id=${RUN_ID}"
echo "[benchmark] workload=${WORKLOAD_CMD}"
echo "[benchmark] idle_before_sec=${IDLE_BEFORE_SEC} idle_after_sec=${IDLE_AFTER_SEC} warmup_sec=${WARMUP_SEC}"

UV_BIN="$(command -v uv || true)"
if [[ -n "${UV_BIN}" ]]; then
  DAEMON_LAUNCH=("${UV_BIN}" "run" "python")
else
  echo "[benchmark] warning: 'uv' not found in user PATH; falling back to system python3" >&2
  DAEMON_LAUNCH=("python3")
fi

write_run_metadata() {
  python3 -c "
import json, os, pathlib, time
run_dir = pathlib.Path(os.environ['RUN_DIR'])
payload = {
    'profile': os.environ['BM_PROFILE'],
    'mode': os.environ['BM_MODE'],
    'run_id': os.environ['BM_RUN_ID'],
    'workload_cmd': os.environ['BM_WORKLOAD'],
    'run_dir': str(run_dir),
    'idle_before_sec': float(os.environ['BM_IDLE_BEFORE']),
    'idle_after_sec': float(os.environ['BM_IDLE_AFTER']),
    'warmup_sec': float(os.environ['BM_WARMUP_SEC']),
    'profile_warmup_sec': float(os.environ['BM_PROFILE_WARMUP']),
    'profile_duration_sec': float(os.environ['BM_PROFILE_DURATION']),
}
for k in ('warmup_started_unix_s', 'warmup_ended_unix_s', 'workload_started_unix_s', 'workload_ended_unix_s'):
    v = os.environ.get(k)
    if v:
        payload[k] = float(v)
(run_dir / 'run_metadata.json').write_text(json.dumps(payload, indent=2), encoding='utf-8')
"
}

export RUN_DIR="${RUN_DIR}"
export BM_PROFILE="${PROFILE}"
export BM_MODE="${MODE}"
export BM_RUN_ID="${RUN_ID}"
export BM_WORKLOAD="${WORKLOAD_CMD}"
export BM_IDLE_BEFORE="${IDLE_BEFORE_SEC}"
export BM_IDLE_AFTER="${IDLE_AFTER_SEC}"
export BM_WARMUP_SEC="${WARMUP_SEC}"
export BM_PROFILE_WARMUP="${WARMUP_SEC}"
export BM_PROFILE_DURATION="${DURATION_SEC}"
unset warmup_started_unix_s warmup_ended_unix_s workload_started_unix_s workload_ended_unix_s

if [[ "${MODE}" == "workload_only" ]]; then
  set +e
  python3 "${SAMPLER}" --output "${RUN_DIR}/summary.jsonl" --window-sec 1.0 >"${RUN_DIR}/sampler.log" 2>&1 &
  SPID=$!
  sleep "${IDLE_BEFORE_SEC}"
  if python3 -c "import sys; sys.exit(0 if float(sys.argv[1]) > 0 else 1)" "${WARMUP_SEC}" 2>/dev/null; then
    export warmup_started_unix_s="$(python3 -c "import time; print(round(time.time(), 6))")"
    timeout "$(python3 -c "import math; print(max(1, int(math.ceil(float('${WARMUP_SEC}')))))")" bash -lc "${WORKLOAD_CMD}" >>"${RUN_DIR}/warmup.log" 2>&1 || true
    export warmup_ended_unix_s="$(python3 -c "import time; print(round(time.time(), 6))")"
  fi
  export workload_started_unix_s="$(python3 -c "import time; print(round(time.time(), 6))")"
  bash -lc "${WORKLOAD_CMD}" >"${RUN_DIR}/workload.log" 2>&1
  export workload_ended_unix_s="$(python3 -c "import time; print(round(time.time(), 6))")"
  sleep "${IDLE_AFTER_SEC}"
  kill "${SPID}" 2>/dev/null
  wait "${SPID}" 2>/dev/null
  set -e
  write_run_metadata
  echo "[benchmark] outputs:"
  echo "  ${RUN_DIR}/summary.jsonl"
  echo "  ${RUN_DIR}/workload.log"
  echo "  ${RUN_DIR}/sampler.log"
  exit 0
fi

DAEMON_COMMON=(
  "daemon/main.py"
  "--run-id" "${RUN_ID}"
  "--run-dir" "${RUN_DIR}"
  "--policy-file" "${POLICY_FILE}"
  "--tuner-catalog" "${CATALOG}"
  "--controller-mode" "${MODE}"
)

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
sleep "${IDLE_BEFORE_SEC}"
if python3 -c "import sys; sys.exit(0 if float(sys.argv[1]) > 0 else 1)" "${WARMUP_SEC}" 2>/dev/null; then
  export warmup_started_unix_s="$(python3 -c "import time; print(round(time.time(), 6))")"
  timeout "$(python3 -c "import math; print(max(1, int(math.ceil(float('${WARMUP_SEC}')))))")" bash -lc "${WORKLOAD_CMD}" >>"${RUN_DIR}/warmup.log" 2>&1 || true
  export warmup_ended_unix_s="$(python3 -c "import time; print(round(time.time(), 6))")"
fi
export workload_started_unix_s="$(python3 -c "import time; print(round(time.time(), 6))")"
bash -lc "${WORKLOAD_CMD}" >"${RUN_DIR}/workload.log" 2>&1
export workload_ended_unix_s="$(python3 -c "import time; print(round(time.time(), 6))")"
sleep "${IDLE_AFTER_SEC}"
sudo kill "${DPID}" 2>/dev/null
wait "${DPID}" 2>/dev/null
set -e

write_run_metadata

echo "[benchmark] outputs:"
echo "  ${RUN_DIR}/summary.jsonl"
echo "  ${RUN_DIR}/decisions.jsonl"
echo "  ${RUN_DIR}/workload.log"
echo "  ${RUN_DIR}/daemon.log"
