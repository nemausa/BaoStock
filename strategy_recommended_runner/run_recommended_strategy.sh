#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
PYTHON_BIN="${PYTHON_BIN:-/home/nemausa/venv/a-stock/bin/python}"

if [[ $# -lt 2 || $# -gt 5 ]]; then
  echo "用法: $0 START_DATE END_DATE [OUTPUT_XLSX] [MODE] [SNAPSHOT_SUFFIX]"
  echo "模式: current | stable-a | stable-b"
  echo "示例: $0 2025-01-01 2025-12-31"
  echo "示例: $0 2025-01-01 2025-12-31 stable-b"
  echo "示例: $0 2025-01-01 2025-12-31 current rebound_f10"
  exit 2
fi

START_DATE="$1"
END_DATE="$2"
START_KEY="${START_DATE//-/}"
END_KEY="${END_DATE//-/}"

MODE="${STRATEGY_MODE:-current}"
SNAPSHOT_SUFFIX="${SNAPSHOT_SUFFIX:-}"
OUTPUT=""
if [[ $# -ge 3 ]]; then
  case "$3" in
    current|stable-a|stable-b)
      MODE="$3"
      ;;
    *)
      OUTPUT="$3"
      ;;
  esac
fi
if [[ $# -eq 4 ]]; then
  case "$4" in
    current|stable-a|stable-b)
      MODE="$4"
      ;;
    *)
      SNAPSHOT_SUFFIX="$4"
      ;;
  esac
fi
if [[ $# -eq 5 ]]; then
  MODE="$4"
  SNAPSHOT_SUFFIX="$5"
fi

case "$MODE" in
  current)
    FILTER_ARGS=()
    DEFAULT_OUTPUT="strategy_recommended_runner/outputs/portfolio_recommended_${START_KEY}_${END_KEY}.xlsx"
    ;;
  stable-a)
    FILTER_ARGS=(--min-current-drawdown 28 --min-rank-score 80)
    DEFAULT_OUTPUT="strategy_recommended_runner/outputs/portfolio_stable_a_${START_KEY}_${END_KEY}.xlsx"
    ;;
  stable-b)
    FILTER_ARGS=(--min-current-drawdown 28 --min-rank-score 85)
    DEFAULT_OUTPUT="strategy_recommended_runner/outputs/portfolio_stable_b_${START_KEY}_${END_KEY}.xlsx"
    ;;
  *)
    echo "未知模式: $MODE"
    echo "可选模式: current | stable-a | stable-b"
    exit 2
    ;;
esac

OUTPUT="${OUTPUT:-$DEFAULT_OUTPUT}"
SNAPSHOT_ARGS=()
if [[ -n "$SNAPSHOT_SUFFIX" ]]; then
  SNAPSHOT_ARGS=(--snapshot-suffix "$SNAPSHOT_SUFFIX")
fi

OPEN_FILTER_ARGS=()
if [[ -n "${MIN_CLOSE_DRAWDOWN:-}" ]]; then
  OPEN_FILTER_ARGS+=(--min-close-drawdown "$MIN_CLOSE_DRAWDOWN")
fi
if [[ -n "${MAX_CLOSE_DRAWDOWN:-}" ]]; then
  OPEN_FILTER_ARGS+=(--max-close-drawdown "$MAX_CLOSE_DRAWDOWN")
fi
if [[ -n "${MAX_NEXT_OPEN_TO_SIGNAL_CLOSE_PCT:-}" ]]; then
  OPEN_FILTER_ARGS+=(--max-next-open-to-signal-close-pct "$MAX_NEXT_OPEN_TO_SIGNAL_CLOSE_PCT")
fi
if [[ -n "${MAX_NEXT_OPEN_TO_CURRENT_LOW_PCT:-}" ]]; then
  OPEN_FILTER_ARGS+=(--max-next-open-to-current-low-pct "$MAX_NEXT_OPEN_TO_CURRENT_LOW_PCT")
fi

cd "$REPO_ROOT"
mkdir -p "$(dirname "$OUTPUT")"

"$PYTHON_BIN" strategy_recommended_runner/scripts/verification_records.py simulate-portfolio \
  --start-date "$START_DATE" \
  --end-date "$END_DATE" \
  --selection-mode all \
  --min-latest-turn 8 \
  "${FILTER_ARGS[@]}" \
  --max-current-drawdown 29 \
  "${OPEN_FILTER_ARGS[@]}" \
  --max-daily-candidates 2 \
  --max-positions 1 \
  --max-daily-buys 1 \
  --take-profit 0.08 \
  --stop-loss 0.03 \
  --max-hold-days 15 \
  --reentry-cooldown-days 10 \
  --initial-capital 50000 \
  --lot-size 100 \
  --position-sizing all-cash \
  "${SNAPSHOT_ARGS[@]}" \
  --output "$OUTPUT" \
  --report-md ""

echo "策略模式: $MODE"
if [[ -n "$SNAPSHOT_SUFFIX" ]]; then
  echo "快照后缀: $SNAPSHOT_SUFFIX"
fi
if [[ -n "${MIN_CLOSE_DRAWDOWN:-}" ]]; then
  echo "收盘回撤过滤: close_drawdown_pct >= ${MIN_CLOSE_DRAWDOWN}%"
fi
if [[ -n "${MAX_CLOSE_DRAWDOWN:-}" ]]; then
  echo "收盘回撤过滤: close_drawdown_pct <= ${MAX_CLOSE_DRAWDOWN}%"
fi
if [[ -n "${MAX_NEXT_OPEN_TO_SIGNAL_CLOSE_PCT:-}" ]]; then
  echo "开盘过滤: 次日开盘相对信号日收盘 <= ${MAX_NEXT_OPEN_TO_SIGNAL_CLOSE_PCT}%"
fi
if [[ -n "${MAX_NEXT_OPEN_TO_CURRENT_LOW_PCT:-}" ]]; then
  echo "开盘过滤: 次日开盘相对当前低点 <= ${MAX_NEXT_OPEN_TO_CURRENT_LOW_PCT}%"
fi
echo "已生成: $REPO_ROOT/$OUTPUT"
