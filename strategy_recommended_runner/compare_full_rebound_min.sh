#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
PYTHON_BIN="${PYTHON_BIN:-/home/nemausa/venv/a-stock/bin/python}"
START_DATE="${1:-2021-01-01}"
END_DATE="${2:-2025-12-31}"

cd "$REPO_ROOT"

declare -a CONFIGS=(
  "rebound_f08:0.08"
  "rebound_f10:0.10"
  "rebound_f15:0.15"
  "rebound_f20:0.20"
)

for config in "${CONFIGS[@]}"; do
  suffix="${config%%:*}"
  full_rebound_min="${config##*:}"
  echo "生成 daily_replay: suffix=${suffix}, full_rebound_min=${full_rebound_min}"
  "$PYTHON_BIN" strategy_recommended_runner/scripts/verification_records.py backfill-daily \
    --start-date "$START_DATE" \
    --end-date "$END_DATE" \
    --suffix "$suffix" \
    --full-rebound-min "$full_rebound_min" \
    --skip-existing \
    --no-excel-snapshot \
    --progress
done

for config in "${CONFIGS[@]}"; do
  suffix="${config%%:*}"
  full_rebound_min="${config##*:}"
  for year in 2021 2022 2023 2024 2025; do
    ./strategy_recommended_runner/run_recommended_strategy.sh \
      "${year}-01-01" \
      "${year}-12-31" \
      "strategy_recommended_runner/outputs/full_rebound_${suffix}_${year}.xlsx" \
      current \
      "$suffix"
  done
  ./strategy_recommended_runner/run_recommended_strategy.sh \
    "$START_DATE" \
    "$END_DATE" \
    "strategy_recommended_runner/outputs/full_rebound_${suffix}_continuous.xlsx" \
    current \
    "$suffix"
done

"$PYTHON_BIN" strategy_recommended_runner/scripts/summarize_full_rebound_min.py

echo "已生成: $REPO_ROOT/strategy_recommended_runner/outputs/full_rebound_min_comparison_2021_2025.xlsx"
