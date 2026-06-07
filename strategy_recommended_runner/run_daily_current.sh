#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
PYTHON_BIN="${PYTHON_BIN:-/home/nemausa/venv/a-stock/bin/python}"

cd "$REPO_ROOT"

"$PYTHON_BIN" daily_run.py --skip-verify-all --suffix daily "$@"

echo "已生成候选排行: $REPO_ROOT/tomorrow_rebound_probability_ranking.xlsx"
echo "f20 口径: full_rebound_min=0.20, current_rebound_max=0.05"
echo "按 current 策略筛选: latest_turn >= 8 且 current_drawdown_pct <= 29，然后看过滤后的 rank 前 2 个候选。"
