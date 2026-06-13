#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
PYTHON_BIN="${PYTHON_BIN:-python3}"

TODAY=$(date +%Y-%m-%d)

cd "$REPO_ROOT"

echo "=== 步骤 1/3: 更新今日行情 ==="
"$PYTHON_BIN" strategy_recommended_runner/scripts/update_a_stock_pytdx.py

echo ""
echo "=== 步骤 2/3: rebound_top3 参数回放今日 ($TODAY) ==="
"$PYTHON_BIN" strategy_recommended_runner/scripts/verification_records.py backfill-daily \
  --start-date "$TODAY" \
  --end-date "$TODAY" \
  --suffix rebound_top3 \
  --current-rebound-max 0.05 \
  --full-rebound-min 0.20 \
  --drawdown-min 0.27 \
  --drawdown-max 0.33 \
  --trend-threshold 0.07 \
  --progress

echo ""
echo "=== 步骤 3/3: 导出候选排行 ==="
"$PYTHON_BIN" strategy_recommended_runner/scripts/export_rebound_top3_ranking.py --date "$TODAY"

echo ""
echo "快照目录: $REPO_ROOT/a_stock_data/verification_records/daily_replay/${TODAY}_rebound_top3/"
echo "排行输出: $REPO_ROOT/strategy_recommended_runner/outputs/rebound_top3_ranking_today.xlsx"
