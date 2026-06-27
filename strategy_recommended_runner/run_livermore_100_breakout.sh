#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
PYTHON_BIN="${PYTHON_BIN:-python3}"
SIGNAL_DATE="$(date +%F)"
SKIP_UPDATE=0
REBUILD_EVENTS=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --date)
      SIGNAL_DATE="$2"
      shift 2
      ;;
    --skip-update)
      SKIP_UPDATE=1
      shift
      ;;
    --rebuild-events)
      REBUILD_EVENTS=1
      shift
      ;;
    -h|--help)
      cat <<'EOF'
用法:
  ./strategy_recommended_runner/run_livermore_100_breakout.sh [YYYY-MM-DD] [--skip-update] [--rebuild-events]
  ./strategy_recommended_runner/run_livermore_100_breakout.sh --date YYYY-MM-DD [--skip-update] [--rebuild-events]

默认流程:
  1. 更新 A 股日线 parquet
  2. 快速扫描指定信号日的 100 关键点首次突破候选

参数:
  --skip-update     跳过行情更新
  --rebuild-events  额外全量重建 livermore_key_levels/events.csv 和 signals.csv（耗时较久）
EOF
      exit 0
      ;;
    *)
      if [[ "$1" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}$ ]]; then
        SIGNAL_DATE="$1"
        shift
      else
        echo "未知参数: $1" >&2
        exit 1
      fi
      ;;
  esac
done

cd "${PROJECT_ROOT}"

if [[ "${SKIP_UPDATE}" -eq 0 ]]; then
  echo "=== 步骤 1/2: 更新 A 股日线数据 ==="
  "${PYTHON_BIN}" strategy_recommended_runner/scripts/update_a_stock_pytdx.py
else
  echo "=== 步骤 1/2: 跳过行情更新 ==="
fi

echo ""
if [[ "${REBUILD_EVENTS}" -eq 1 ]]; then
  echo "=== 可选步骤: 全量重建利弗莫尔关键点 events/signals（耗时较久） ==="
  "${PYTHON_BIN}" strategy_recommended_runner/scripts/analyze_livermore_key_levels.py
else
  echo "=== 步骤 2/2: 使用快速扫描，不重建全量 events/signals ==="
fi

echo ""
echo "=== 导出 ${SIGNAL_DATE} 的 100 关键点首次突破候选 ==="
"${PYTHON_BIN}" strategy_recommended_runner/scripts/export_key_level_100_breakout_candidate_fast.py \
  --date "${SIGNAL_DATE}"
