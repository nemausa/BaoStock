from __future__ import annotations

from pathlib import Path

import pandas as pd


OUT_DIR = Path("strategy_recommended_runner/outputs")
OUTPUT = OUT_DIR / "full_rebound_min_comparison_2021_2025.xlsx"
CONFIGS = [
    ("rebound_f08", 0.08),
    ("rebound_f10", 0.10),
    ("rebound_f15", 0.15),
    ("rebound_f20", 0.20),
]
YEARS = [2021, 2022, 2023, 2024, 2025]


def read_summary(path: Path) -> dict[str, object]:
    df = pd.read_excel(path, sheet_name="资金汇总")
    return dict(zip(df["项目"], df["值"]))


def numeric(data: dict[str, object], key: str, default: float = 0.0) -> float:
    value = data.get(key, default)
    try:
        return float(value)
    except Exception:
        return default


def integer(data: dict[str, object], key: str, default: int = 0) -> int:
    value = data.get(key, default)
    try:
        return int(value)
    except Exception:
        return default


def main() -> None:
    yearly_rows: list[dict[str, object]] = []
    continuous_rows: list[dict[str, object]] = []

    for suffix, full_rebound_min in CONFIGS:
        for year in YEARS:
            path = OUT_DIR / f"full_rebound_{suffix}_{year}.xlsx"
            data = read_summary(path)
            yearly_rows.append({
                "suffix": suffix,
                "full_rebound_min": full_rebound_min,
                "year": year,
                "initial_capital": numeric(data, "initial_capital"),
                "final_asset": numeric(data, "final_asset"),
                "total_profit": numeric(data, "total_profit"),
                "total_return_pct": numeric(data, "total_return_pct"),
                "trade_count": integer(data, "trade_count"),
                "win_count": integer(data, "win_count"),
                "win_rate_pct": numeric(data, "win_rate_pct"),
                "max_drawdown_pct": numeric(data, "max_drawdown_pct"),
                "avg_trade_pnl": numeric(data, "avg_trade_pnl"),
                "avg_trade_return_pct": numeric(data, "avg_trade_return_pct"),
                "fees": data.get("fees", ""),
            })

        path = OUT_DIR / f"full_rebound_{suffix}_continuous.xlsx"
        data = read_summary(path)
        continuous_rows.append({
            "suffix": suffix,
            "full_rebound_min": full_rebound_min,
            "initial_capital": numeric(data, "initial_capital"),
            "final_asset": numeric(data, "final_asset"),
            "total_profit": numeric(data, "total_profit"),
            "total_return_pct": numeric(data, "total_return_pct"),
            "trade_count": integer(data, "trade_count"),
            "win_count": integer(data, "win_count"),
            "win_rate_pct": numeric(data, "win_rate_pct"),
            "max_drawdown_pct": numeric(data, "max_drawdown_pct"),
            "avg_trade_pnl": numeric(data, "avg_trade_pnl"),
            "avg_trade_return_pct": numeric(data, "avg_trade_return_pct"),
            "fees": data.get("fees", ""),
        })

    yearly = pd.DataFrame(yearly_rows)
    continuous = pd.DataFrame(continuous_rows)
    mode_summary = yearly.groupby(["suffix", "full_rebound_min"]).agg(
        positive_years=("total_return_pct", lambda s: int((s > 0).sum())),
        avg_return_pct=("total_return_pct", "mean"),
        min_return_pct=("total_return_pct", "min"),
        sum_return_pct=("total_return_pct", "sum"),
        total_trades=("trade_count", "sum"),
        avg_win_rate_pct=("win_rate_pct", "mean"),
        worst_drawdown_pct=("max_drawdown_pct", "min"),
    ).reset_index()
    yearly_return = yearly.pivot(
        index="year",
        columns="full_rebound_min",
        values="total_return_pct",
    ).reset_index()
    yearly_trades = yearly.pivot(
        index="year",
        columns="full_rebound_min",
        values="trade_count",
    ).reset_index()

    params = pd.DataFrame([
        ("策略", "current 原策略"),
        ("买入", "信号日后一个交易日开盘买入"),
        ("选股", "latest_turn >= 8 且 current_drawdown_pct <= 29，每日候选最多 2，只持仓 1 只"),
        ("卖出", "止盈 8%，止损 3%，最多持有 15 个交易日"),
        ("本金", "50000，按 100 股整数手买满，手续费暂不扣除"),
        ("对比变量", "full_rebound_min = 0.08 / 0.10 / 0.15 / 0.20"),
    ], columns=["项目", "说明"])

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    with pd.ExcelWriter(OUTPUT) as writer:
        yearly_return.to_excel(writer, index=False, sheet_name="年度收益率对比")
        yearly.to_excel(writer, index=False, sheet_name="年度资金汇总")
        continuous.to_excel(writer, index=False, sheet_name="连续运行汇总")
        mode_summary.to_excel(writer, index=False, sheet_name="阈值汇总")
        yearly_trades.to_excel(writer, index=False, sheet_name="年度交易数对比")
        params.to_excel(writer, index=False, sheet_name="参数说明")

    print(f"已生成 full_rebound_min 对比: {OUTPUT.resolve()}")
    print("年度收益率对比")
    print(yearly_return.to_string(index=False))
    print("连续运行汇总")
    print(continuous.to_string(index=False))


if __name__ == "__main__":
    main()
