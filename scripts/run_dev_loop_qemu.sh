#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="${SCRIPT_DIR%/scripts}"
cd "${REPO_ROOT}"

PROFILE="${1:-cpu_bound}"
STAMP="$(date +%Y%m%d-%H%M%S)"
BASE_RUN_ID="devloop-${PROFILE}-baseline-${STAMP}"
TUNED_RUN_ID="devloop-${PROFILE}-tuned-${STAMP}"

export REFLEX_VM_CACHE="${REPO_ROOT}/data/qemu"
mkdir -p "${REFLEX_VM_CACHE}" "${REPO_ROOT}/data/runs"

echo "[dev-loop] persisted VM cache: ${REFLEX_VM_CACHE}"
echo "[dev-loop] starting QEMU smoke setup"
bash "${REPO_ROOT}/scripts/test_mvp_qemu.sh"

echo "[dev-loop] running baseline profile"
bash "${REPO_ROOT}/benchmarks/run_profile.sh" "${PROFILE}" baseline "${BASE_RUN_ID}"
echo "[dev-loop] running tuned profile"
bash "${REPO_ROOT}/benchmarks/run_profile.sh" "${PROFILE}" tuned "${TUNED_RUN_ID}"

BASE_SUMMARY="${REPO_ROOT}/data/runs/${BASE_RUN_ID}/summary.jsonl"
TUNED_SUMMARY="${REPO_ROOT}/data/runs/${TUNED_RUN_ID}/summary.jsonl"
SCORECARD="${REPO_ROOT}/data/runs/devloop-${PROFILE}-${STAMP}-scorecard.json"

python "${REPO_ROOT}/benchmarks/scorecard.py" "${BASE_SUMMARY}" "${TUNED_SUMMARY}" >"${SCORECARD}"

echo "[dev-loop] complete"
echo "  baseline: ${REPO_ROOT}/data/runs/${BASE_RUN_ID}"
echo "  tuned:    ${REPO_ROOT}/data/runs/${TUNED_RUN_ID}"
echo "  scorecard:${SCORECARD}"
