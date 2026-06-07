#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
PYTHON_BIN="${PYTHON_BIN:-/home/nemausa/venv/a-stock/bin/python}"

if [[ $# -lt 2 || $# -gt 5 ]]; then
  echo "用法: $0 START_DATE END_DATE [SUFFIX] [FULL_REBOUND_MIN] [CURRENT_REBOUND_MAX]"
  echo "示例: $0 2026-01-01 2026-04-30"
  echo "示例: $0 2021-01-01 2025-12-31 rebound_f10 0.10 0.05"
  exit 2
fi

START_DATE="$1"
END_DATE="$2"
SUFFIX="${3:-rebound_top3}"
FULL_REBOUND_MIN="${4:-0.20}"
CURRENT_REBOUND_MAX="${5:-0.05}"

cd "$REPO_ROOT"

"$PYTHON_BIN" strategy_recommended_runner/scripts/verification_records.py backfill-daily \
  --start-date "$START_DATE" \
  --end-date "$END_DATE" \
  --suffix "$SUFFIX" \
  --current-rebound-max "$CURRENT_REBOUND_MAX" \
  --full-rebound-min "$FULL_REBOUND_MIN" \
  --skip-existing \
  --progress

echo "每日回放数据目录: $REPO_ROOT/a_stock_data/verification_records/daily_replay"
echo "快照后缀: $SUFFIX"
echo "full_rebound_min: $FULL_REBOUND_MIN"
echo "current_rebound_max: $CURRENT_REBOUND_MAX"
