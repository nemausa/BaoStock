from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd

import verification_records as vr


OUTPUT_DIR = Path("strategy_recommended_runner/outputs")


PROFILES = [
    ("原始 f20", None, None),
    ("close_drawdown 25%-33%", 25.0, 33.0),
    ("close_drawdown 27%-33%", 27.0, 33.0),
    ("close_drawdown 27%-31%", 27.0, 31.0),
    ("close_drawdown 28%-33%", 28.0, 33.0),
    ("close_drawdown 28%-31%", 28.0, 31.0),
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="对比 f20 current 策略增加收盘回撤过滤后的表现。")
    parser.add_argument("--start-date", default="2026-01-01")
    parser.add_argument("--end-date", default="2026-04-30")
    parser.add_argument("--snapshot-suffix", default="rebound_top3")
    parser.add_argument("--initial-capital", type=float, default=50000.0)
    parser.add_argument("--daily-dir", type=Path, default=vr.DAILY_REPLAY_DIR)
    parser.add_argument("--output-xlsx", type=Path, default=OUTPUT_DIR / "f20_close_drawdown_202601_202604.xlsx")
    parser.add_argument("--output-md", type=Path, default=OUTPUT_DIR / "f20_close_drawdown_202601_202604.md")
    return parser.parse_args()


def simulation_args(args: argparse.Namespace, min_close: float | None, max_close: float | None) -> argparse.Namespace:
    return argparse.Namespace(
        command="simulate-portfolio",
        start_date=args.start_date,
        end_date=args.end_date,
        selection_mode="all",
        top_n=2,
        min_rank_score=None,
        min_probability_score=None,
        min_latest_turn=8.0,
        min_current_drawdown=None,
        max_current_drawdown=29.0,
        max_latest_pct_chg=None,
        min_latest_pct_chg=None,
        max_low_to_latest_pct=None,
        min_close_drawdown=min_close,
        max_close_drawdown=max_close,
        max_next_open_to_signal_close_pct=None,
        max_next_open_to_current_low_pct=None,
        max_rank=None,
        max_daily_candidates=2,
        max_positions=1,
        max_daily_buys=1,
        take_profit=0.08,
        stop_loss=0.03,
        max_hold_days=15,
        reentry_cooldown_days=10,
        initial_capital=float(args.initial_capital),
        lot_size=100,
        position_sizing="all-cash",
        daily_dir=args.daily_dir,
        snapshot_suffix=args.snapshot_suffix,
        output=None,
        report_md=None,
    )


def summary_value(summary: pd.DataFrame, key: str, default: object = None) -> object:
    if summary.empty:
        return default
    matched = summary[summary["项目"] == key]
    if matched.empty:
        return default
    return matched["值"].iloc[0]


def simulate_profile(args: argparse.Namespace, label: str, min_close: float | None, max_close: float | None) -> dict[str, object]:
    sim_args = simulation_args(args, min_close, max_close)
    daily_rankings, skipped_snapshots = vr.load_daily_rankings(sim_args)
    if daily_rankings.empty:
        raise RuntimeError(f"没有 daily_replay 数据: {args.start_date} 到 {args.end_date}")

    candidates = vr.select_portfolio_candidates(daily_rankings, sim_args)
    price_cache: dict[str, pd.DataFrame] = {}
    if candidates.empty:
        orders = pd.DataFrame()
        skipped_before_orders = pd.DataFrame()
    else:
        orders, skipped_before_orders = vr.build_portfolio_orders(candidates, price_cache, sim_args)

    cash_trades, cash_daily_assets, cash_skipped_during, _ = vr.simulate_cash_portfolio(orders, price_cache, sim_args)
    skipped_parts = [skipped_snapshots, skipped_before_orders, cash_skipped_during]
    skipped = (
        pd.concat([part for part in skipped_parts if not part.empty], ignore_index=True)
        if any(not part.empty for part in skipped_parts)
        else pd.DataFrame()
    )
    summary = vr.cash_portfolio_summary(cash_trades, cash_daily_assets, skipped, sim_args)

    return {
        "条件": label,
        "start_date": args.start_date,
        "end_date": args.end_date,
        "min_close_drawdown": min_close,
        "max_close_drawdown": max_close,
        "候选数": int(len(candidates)),
        "订单数": int(len(orders)),
        "实际交易数": int(summary_value(summary, "trade_count", 0) or 0),
        "胜率_pct": float(summary_value(summary, "win_rate_pct", 0.0) or 0.0),
        "总收益": float(summary_value(summary, "total_profit", 0.0) or 0.0),
        "收益率_pct": float(summary_value(summary, "total_return_pct", 0.0) or 0.0),
        "最终资金": float(summary_value(summary, "final_asset", args.initial_capital) or args.initial_capital),
        "最大回撤_pct": float(summary_value(summary, "max_drawdown_pct", 0.0) or 0.0),
        "平均单笔收益": float(summary_value(summary, "avg_trade_pnl", 0.0) or 0.0),
        "平均单笔收益率_pct": float(summary_value(summary, "avg_trade_return_pct", 0.0) or 0.0),
        "跳过信号数": int(summary_value(summary, "skipped_count", 0) or 0),
    }


def fmt_pct(value: object) -> str:
    return f"{float(value):+.2f}%"


def fmt_money(value: object) -> str:
    return f"{float(value):,.2f}"


def write_markdown(path: Path, result: pd.DataFrame, args: argparse.Namespace) -> None:
    lines = [
        "# f20 收盘回撤过滤对比",
        "",
        "## 固定规则",
        "",
        f"- 区间: `{args.start_date}` 到 `{args.end_date}`",
        "- 信号快照: `rebound_top3`",
        "- 基础选股: `latest_turn >= 8`, `current_drawdown_pct <= 29`, 每日最多 2 个候选",
        "- 买入: 次日开盘, 最多持仓 1 只, 可用现金买满整手",
        "- 卖出: 止盈 8%, 止损 3%, 最多持有 15 个交易日, 同股冷却 10 个交易日",
        "",
        "## 对比结果",
        "",
        "| 条件 | 候选数 | 交易数 | 胜率 | 总收益 | 收益率 | 最终资金 | 最大回撤 | 平均单笔收益 | 跳过信号数 |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for _, row in result.iterrows():
        lines.append(
            "| "
            + " | ".join([
                str(row["条件"]),
                str(int(row["候选数"])),
                str(int(row["实际交易数"])),
                fmt_pct(row["胜率_pct"]),
                fmt_money(row["总收益"]),
                fmt_pct(row["收益率_pct"]),
                fmt_money(row["最终资金"]),
                fmt_pct(row["最大回撤_pct"]),
                fmt_money(row["平均单笔收益"]),
                str(int(row["跳过信号数"])),
            ])
            + " |"
        )
    lines.extend([
        "",
        "## 说明",
        "",
        "- `close_drawdown_pct = (current_top_price - latest_close) / current_top_price * 100`。",
        "- 它按信号日收盘价衡量回撤，比 `current_drawdown_pct` 更接近晚上选股、次日开盘买入的执行口径。",
    ])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    args = parse_args()
    rows = []
    for label, min_close, max_close in PROFILES:
        print(f"计算: {label}", flush=True)
        rows.append(simulate_profile(args, label, min_close, max_close))

    result = pd.DataFrame(rows)
    args.output_xlsx.parent.mkdir(parents=True, exist_ok=True)
    with pd.ExcelWriter(args.output_xlsx) as writer:
        result.to_excel(writer, index=False, sheet_name="对比结果")

    write_markdown(args.output_md, result, args)
    print(f"已生成: {args.output_xlsx.resolve()}")
    print(f"已生成: {args.output_md.resolve()}")
    print(result.to_string(index=False))


if __name__ == "__main__":
    main()
