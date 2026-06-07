from __future__ import annotations

import argparse
from datetime import date
from pathlib import Path

import pandas as pd


DAILY_REPLAY_DIR = Path("a_stock_data/verification_records/daily_replay")
OUTPUT_DIR = Path("strategy_recommended_runner/outputs")

DISPLAY_COLUMNS = [
    "rank",
    "code",
    "name",
    "rank_score",
    "probability_score",
    "current_drawdown_pct",
    "current_low_to_latest_pct",
    "latest_turn",
    "latest_pct_chg",
    "current_stage",
    "current_is_bottom_confirmed",
    "latest_close",
    "current_low_price",
    "current_top_price",
    "history_success_rate_pct",
    "history_sample_count",
    "history_success_count",
    "reason",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="从 daily_replay rebound_top3 快照导出当日全量排行。")
    parser.add_argument(
        "--date",
        default=date.today().strftime("%Y-%m-%d"),
        help="快照日期，格式 YYYY-MM-DD，默认今日",
    )
    parser.add_argument("--daily-dir", type=Path, default=DAILY_REPLAY_DIR)
    parser.add_argument("--output-xlsx", type=Path, default=OUTPUT_DIR / "rebound_top3_ranking_today.xlsx")
    parser.add_argument("--output-csv", type=Path, default=OUTPUT_DIR / "rebound_top3_ranking_today.csv")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    snapshot_dir = args.daily_dir / f"{args.date}_rebound_top3"
    ranking_csv = snapshot_dir / "ranking_snapshot.csv"

    if not ranking_csv.exists():
        raise FileNotFoundError(
            f"没有找到快照: {ranking_csv}\n"
            f"请先运行: ./strategy_recommended_runner/run_daily_rebound_top3.sh"
        )

    df = pd.read_csv(ranking_csv, dtype={"code": str})
    if df.empty:
        print(f"今日 ({args.date}) 没有满足 rebound_top3 条件的候选股。")
        return

    for col in ["rank", "rank_score", "probability_score", "current_drawdown_pct",
                "current_low_to_latest_pct", "latest_turn", "latest_pct_chg"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    df = df.sort_values("rank").reset_index(drop=True)

    output_cols = [c for c in DISPLAY_COLUMNS if c in df.columns]
    display_df = df[output_cols].copy()

    args.output_xlsx.parent.mkdir(parents=True, exist_ok=True)
    with pd.ExcelWriter(args.output_xlsx) as writer:
        pd.DataFrame([
            ("数据日期", args.date),
            ("候选数量", len(df)),
            ("生成时间", pd.Timestamp.now().strftime("%Y-%m-%d %H:%M:%S")),
        ], columns=["项目", "说明"]).to_excel(writer, index=False, sheet_name="日期")
        display_df.to_excel(writer, index=False, sheet_name="候选排行")
        df.to_excel(writer, index=False, sheet_name="完整数据")
        pd.DataFrame([
            ("数据日期", args.date),
            ("候选数", len(df)),
            ("策略", "rebound_top3"),
            ("current_rebound_max", "5%（距低点涨幅 ≤ 5%）"),
            ("full_rebound_min", "20%（历史有效反弹 ≥ 20%）"),
            ("drawdown_min/max", "27% ~ 33%"),
            ("trend_threshold", "7%"),
            ("排序", "rank_score 降序（rank 升序）"),
        ], columns=["项目", "说明"]).to_excel(writer, index=False, sheet_name="参数说明")

    display_df.to_csv(args.output_csv, index=False, encoding="utf-8-sig")

    print(f"信号日期: {args.date}")
    print(f"候选数量: {len(df)}")
    print()

    preview_cols = [c for c in ["rank", "code", "name", "rank_score", "probability_score",
                                 "current_drawdown_pct", "current_low_to_latest_pct",
                                 "latest_turn", "latest_pct_chg", "current_stage"] if c in display_df.columns]
    print(display_df[preview_cols].to_string(index=False, max_colwidth=60))
    print()
    print(f"已保存: {args.output_xlsx.resolve()}")
    print(f"已保存: {args.output_csv.resolve()}")


if __name__ == "__main__":
    main()
