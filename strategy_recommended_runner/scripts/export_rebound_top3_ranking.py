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

TOP3_COLUMNS = [
    "code",
    "name",
    "rank_score",
    "probability_score",
    "current_drawdown_pct",
    "latest_turn",
    "current_low_to_latest_pct",
    "history_success_rate_pct",
    "latest_close",
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
    parser.add_argument("--min-turn", type=float, default=5.0, help="换手率下限，默认 5.0%%")
    parser.add_argument("--max-drawdown", type=float, default=33.0, help="回撤上限，默认 33.0%%")
    parser.add_argument("--min-breadth", type=int, default=2,
                        help="信号广度下限：当日候选数 < 此值则不操作，默认 2（要求市场共振）")
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

    # 二次过滤：换手率 + 回撤
    filtered = df[
        (df["latest_turn"] >= args.min_turn) &
        (df["current_drawdown_pct"] <= args.max_drawdown)
    ].sort_values("rank").reset_index(drop=True)

    top3 = filtered.head(3)

    output_cols = [c for c in DISPLAY_COLUMNS if c in df.columns]
    display_df = df[output_cols].copy()

    # ── 控制台输出 ──
    print(f"信号日期: {args.date}")
    print(f"全部候选: {len(df)} 只   过滤后(换手率>={args.min_turn}%, 回撤<={args.max_drawdown}%): {len(filtered)} 只")
    print()

    SEP = "═" * 56
    if top3.empty:
        print(SEP)
        print(f"  今日无满足条件候选（换手率>={args.min_turn}%, 回撤<={args.max_drawdown}%），不操作。")
        print(SEP)
    elif len(filtered) < args.min_breadth:
        # 信号广度不足：当日见底股票太少，市场未共振，不操作
        print(SEP)
        print(f"  ⚠ 今日信号孤立（仅 {len(filtered)} 只满足条件，需 >= {args.min_breadth} 只），市场未共振，不操作。")
        print(f"  （信号广度过滤：要求当日至少 {args.min_breadth} 只股票同时见底才买入）")
        print(SEP)
    else:
        print(SEP)
        print(f"  明日候选（信号广度 {len(filtered)} 只，已达标；只看第1只；低开>2%则当日不操作）")
        for i, (_, row) in enumerate(top3.iterrows(), 1):
            code = row.get("code", "")
            name = row.get("name", "")
            rs = row.get("rank_score", float("nan"))
            dd = row.get("current_drawdown_pct", float("nan"))
            turn = row.get("latest_turn", float("nan"))
            dist = row.get("current_low_to_latest_pct", float("nan"))
            print(f"  #{i}  {code}  {name}   rank_score={rs:.1f}   "
                  f"回撤={dd:.1f}%  换手率={turn:.1f}%  距低={dist:.1f}%")
        print(f"  ⚠ 低开确认：第1只开盘 >= 昨收×0.98 才买；低开>2%则当日不操作")
        print(f"  出场：止损-4%(×0.96) 止盈+10%(×1.10) 最长持仓15个交易日")
        print(SEP)

    print()
    if not filtered.empty:
        print(f"过滤后全部候选（换手率>={args.min_turn}%, 回撤<={args.max_drawdown}%）:")
        preview_cols = [c for c in ["rank", "code", "name", "rank_score", "probability_score",
                                    "current_drawdown_pct", "current_low_to_latest_pct",
                                    "latest_turn", "latest_pct_chg", "current_stage"]
                        if c in filtered.columns]
        print(filtered[preview_cols].to_string(index=False, max_colwidth=60))
        print()

    # ── Excel 输出 ──
    args.output_xlsx.parent.mkdir(parents=True, exist_ok=True)
    with pd.ExcelWriter(args.output_xlsx) as writer:
        # 备选顺序 sheet
        if not top3.empty:
            top3_out = top3[[c for c in TOP3_COLUMNS if c in top3.columns]].copy()
            top3_out.insert(0, "备选", [f"#{i}" for i in range(1, len(top3_out) + 1)])
        else:
            top3_out = pd.DataFrame(columns=["备选"] + [c for c in TOP3_COLUMNS])
        top3_out.to_excel(writer, index=False, sheet_name="备选顺序")

        # 过滤候选 sheet
        if not filtered.empty:
            filtered_out = filtered[[c for c in output_cols if c in filtered.columns]]
        else:
            filtered_out = pd.DataFrame(columns=output_cols)
        filtered_out.to_excel(writer, index=False, sheet_name="过滤候选")

        # 原有全量候选
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
            ("换手率过滤", f">= {args.min_turn}%"),
            ("回撤过滤", f"<= {args.max_drawdown}%"),
            ("信号广度过滤", f"当日候选数 >= {args.min_breadth} 只才交易（不足则空仓）"),
            ("低开过滤", "次日开盘确认：开盘 >= 昨收×0.98 才买"),
            ("出场规则", "止损 -4%(×0.96)；止盈 +10%(×1.10)；最长持仓 15 个交易日"),
            ("备选逻辑", "只买 rank1；rank1 低开>2% 则当日不操作，不回退 rank2/rank3"),
        ], columns=["项目", "说明"]).to_excel(writer, index=False, sheet_name="参数说明")

    display_df.to_csv(args.output_csv, index=False, encoding="utf-8-sig")

    print(f"已保存: {args.output_xlsx.resolve()}")
    print(f"已保存: {args.output_csv.resolve()}")


if __name__ == "__main__":
    main()
