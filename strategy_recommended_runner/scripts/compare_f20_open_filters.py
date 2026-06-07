from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd

import verification_records as vr


OUTPUT_DIR = Path("strategy_recommended_runner/outputs")


PROFILES = [
    {
        "profile": "原始 f20",
        "max_next_open_to_signal_close_pct": None,
        "max_next_open_to_current_low_pct": None,
    },
    {
        "profile": "f20 + 次日开盘<=信号收盘+2%",
        "max_next_open_to_signal_close_pct": 2.0,
        "max_next_open_to_current_low_pct": None,
    },
    {
        "profile": "f20 + 次日开盘<=当前低点+5%",
        "max_next_open_to_signal_close_pct": None,
        "max_next_open_to_current_low_pct": 5.0,
    },
    {
        "profile": "f20 + 双开盘过滤",
        "max_next_open_to_signal_close_pct": 2.0,
        "max_next_open_to_current_low_pct": 5.0,
    },
]


PERIODS = [
    ("2021", "2021-01-01", "2021-12-31"),
    ("2022", "2022-01-01", "2022-12-31"),
    ("2023", "2023-01-01", "2023-12-31"),
    ("2024", "2024-01-01", "2024-12-31"),
    ("2025", "2025-01-01", "2025-12-31"),
    ("2026-01到04", "2026-01-01", "2026-04-30"),
    ("2021-2025连续", "2021-01-01", "2025-12-31"),
    ("2021-2026-04连续", "2021-01-01", "2026-04-30"),
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="对比 f20 current 策略的次日开盘过滤效果。")
    parser.add_argument("--output-xlsx", type=Path, default=OUTPUT_DIR / "f20_open_entry_filter_comparison.xlsx")
    parser.add_argument("--output-md", type=Path, default=OUTPUT_DIR / "f20_open_entry_filter_comparison.md")
    parser.add_argument("--snapshot-suffix", default="rebound_top3")
    parser.add_argument("--initial-capital", type=float, default=50000.0)
    parser.add_argument("--daily-dir", type=Path, default=vr.DAILY_REPLAY_DIR)
    return parser.parse_args()


def base_simulation_args(args: argparse.Namespace, start_date: str, end_date: str, profile: dict[str, object]) -> argparse.Namespace:
    return argparse.Namespace(
        command="simulate-portfolio",
        start_date=start_date,
        end_date=end_date,
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
        max_next_open_to_signal_close_pct=profile["max_next_open_to_signal_close_pct"],
        max_next_open_to_current_low_pct=profile["max_next_open_to_current_low_pct"],
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


def count_open_filter_skips(skipped: pd.DataFrame) -> int:
    if skipped.empty or "skip_reason" not in skipped:
        return 0
    reasons = skipped["skip_reason"].fillna("").astype(str)
    return int(reasons.str.startswith("次日开盘相对").sum())


def cagr_pct(start_date: str, end_date: str, total_return_pct: float) -> float | None:
    days = (pd.Timestamp(end_date) - pd.Timestamp(start_date)).days
    if days <= 0:
        return None
    years = days / 365.25
    if years <= 0:
        return None
    return ((1 + total_return_pct / 100) ** (1 / years) - 1) * 100


def simulate_period(args: argparse.Namespace, period: tuple[str, str, str], profile: dict[str, object]) -> dict[str, object]:
    period_label, start_date, end_date = period
    sim_args = base_simulation_args(args, start_date, end_date, profile)

    daily_rankings, skipped_snapshots = vr.load_daily_rankings(sim_args)
    if daily_rankings.empty:
        raise RuntimeError(f"没有 daily_replay 数据: {period_label} {start_date} 到 {end_date}")

    candidates = vr.select_portfolio_candidates(daily_rankings, sim_args)
    price_cache: dict[str, pd.DataFrame] = {}
    if candidates.empty:
        orders = pd.DataFrame()
        skipped_before_orders = pd.DataFrame()
    else:
        orders, skipped_before_orders = vr.build_portfolio_orders(candidates, price_cache, sim_args)

    cash_trades, cash_daily_assets, cash_skipped_during, cash_summary = vr.simulate_cash_portfolio(orders, price_cache, sim_args)
    cash_skipped_parts = [skipped_snapshots, skipped_before_orders, cash_skipped_during]
    cash_skipped = (
        pd.concat([part for part in cash_skipped_parts if not part.empty], ignore_index=True)
        if any(not part.empty for part in cash_skipped_parts)
        else pd.DataFrame()
    )
    cash_summary = vr.cash_portfolio_summary(cash_trades, cash_daily_assets, cash_skipped, sim_args)

    total_return = float(summary_value(cash_summary, "total_return_pct", 0.0) or 0.0)
    row = {
        "profile": profile["profile"],
        "period": period_label,
        "start_date": start_date,
        "end_date": end_date,
        "candidate_count": int(len(candidates)),
        "order_count": int(len(orders)),
        "trade_count": int(summary_value(cash_summary, "trade_count", 0) or 0),
        "win_rate_pct": float(summary_value(cash_summary, "win_rate_pct", 0.0) or 0.0),
        "total_return_pct": total_return,
        "final_asset": float(summary_value(cash_summary, "final_asset", sim_args.initial_capital) or sim_args.initial_capital),
        "total_profit": float(summary_value(cash_summary, "total_profit", 0.0) or 0.0),
        "max_drawdown_pct": float(summary_value(cash_summary, "max_drawdown_pct", 0.0) or 0.0),
        "skipped_count": int(summary_value(cash_summary, "skipped_count", 0) or 0),
        "open_filter_skipped_count": count_open_filter_skips(skipped_before_orders),
        "cagr_pct": cagr_pct(start_date, end_date, total_return) if "连续" in period_label else None,
    }
    return row


def format_pct(value: object) -> str:
    if value is None or pd.isna(value):
        return ""
    return f"{float(value):+.2f}%"


def format_money(value: object) -> str:
    if value is None or pd.isna(value):
        return ""
    return f"{float(value):,.2f}"


def markdown_table(df: pd.DataFrame) -> str:
    headers = ["参数", "周期", "收益率", "最终资金", "胜率", "最大回撤", "交易数", "开盘过滤跳过"]
    lines = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join(["---"] * len(headers)) + " |",
    ]
    for _, row in df.iterrows():
        lines.append(
            "| "
            + " | ".join([
                str(row["profile"]),
                str(row["period"]),
                format_pct(row["total_return_pct"]),
                format_money(row["final_asset"]),
                format_pct(row["win_rate_pct"]),
                format_pct(row["max_drawdown_pct"]),
                str(int(row["trade_count"])),
                str(int(row["open_filter_skipped_count"])),
            ])
            + " |"
        )
    return "\n".join(lines)


def write_markdown(path: Path, rows: pd.DataFrame) -> None:
    annual = rows[~rows["period"].astype(str).str.contains("连续")].copy()
    continuous = rows[rows["period"].astype(str).str.contains("连续")].copy()

    lines = [
        "# f20 次日开盘过滤对比",
        "",
        "## 固定规则",
        "",
        "- 信号快照: `rebound_top3`, 即 `full_rebound_min = 0.20`, `current_rebound_max = 0.05`",
        "- 选股: `latest_turn >= 8`, `current_drawdown_pct <= 29`, 每日最多看过滤后的前 2 个",
        "- 持仓: 最多 1 只, 可用现金买满整手, 初始本金 50,000",
        "- 卖出: 止盈 8%, 止损 3%, 最多持有 15 个交易日, 同股冷却 10 个交易日",
        "- 买入价: 信号日后的下一个交易日开盘价",
        "",
        "## 年度结果",
        "",
        markdown_table(annual),
        "",
        "## 连续区间结果",
        "",
        markdown_table(continuous),
        "",
        "## 结论口径",
        "",
        "- `开盘过滤跳过` 只统计因为次日开盘价过高而直接放弃的候选。",
        "- 如果开盘过滤提升收益率但交易数明显下降，说明它更像风控条件；如果收益率和回撤同时改善，才适合纳入实盘。",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    args = parse_args()
    rows: list[dict[str, object]] = []
    for profile in PROFILES:
        for period in PERIODS:
            print(f"计算: {profile['profile']} {period[0]}")
            rows.append(simulate_period(args, period, profile))

    result = pd.DataFrame(rows)
    args.output_xlsx.parent.mkdir(parents=True, exist_ok=True)
    with pd.ExcelWriter(args.output_xlsx) as writer:
        result.to_excel(writer, index=False, sheet_name="对比结果")
        result[~result["period"].astype(str).str.contains("连续")].to_excel(writer, index=False, sheet_name="年度")
        result[result["period"].astype(str).str.contains("连续")].to_excel(writer, index=False, sheet_name="连续区间")

    write_markdown(args.output_md, result)
    print(f"已生成: {args.output_xlsx.resolve()}")
    print(f"已生成: {args.output_md.resolve()}")
    print(result.to_string(index=False))


if __name__ == "__main__":
    main()
