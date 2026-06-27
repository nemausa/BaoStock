from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd

from backtest_key_level_trailing_stop import (
    DEFAULT_OUT_DIR,
    DEFAULT_SIGNALS_FILE,
    build_sweep_records,
    fixed_exit_from_future,
    load_signals,
    metrics_for_trades,
    normalize_code,
    select_daily_records,
)


DEFAULT_PREFIX = "key_level_100_events_2025"


def build_params(args: argparse.Namespace) -> dict[str, object]:
    return {
        "stop_pct": args.stop_pct,
        "target_pct": args.target_pct,
        "max_hold_days": args.max_hold_days,
        "gain_range": (args.gain_min, args.gain_max),
        "max_signal_high": args.max_signal_high,
        "close_range": (args.close_min, args.close_max),
        "volume_only": False,
        "preferred_close_min": args.preferred_close_min,
    }


def independent_event_trades(
    records: list[dict[str, object]],
    params: dict[str, object],
    start_date: pd.Timestamp,
    end_date: pd.Timestamp,
) -> pd.DataFrame:
    selected = select_daily_records(records, params, start_date, end_date)
    trades: list[dict[str, object]] = []

    for record in selected:
        exit_result = fixed_exit_from_future(record, params)
        sell_date = pd.Timestamp(exit_result["sell_date"])
        buy_date = pd.Timestamp(record["buy_date"])
        signal_date = pd.Timestamp(record["signal_date"])
        buy_price = float(record["buy_price"])
        sell_price = float(exit_result["sell_price"])
        future = record["future"].head(int(params["max_hold_days"]))
        sell_positions = future.index[future["date"].eq(sell_date)]
        holding_days = int(sell_positions[0] - future.index[0] + 1) if len(sell_positions) else pd.NA

        trades.append(
            {
                "信号日": signal_date.strftime("%Y-%m-%d"),
                "买入日": buy_date.strftime("%Y-%m-%d"),
                "股票代码": normalize_code(record["code"]),
                "股票名称": record["name"],
                "评分": int(record["score"]),
                "D0收盘": float(record["d0_close"]),
                "D0涨幅%": float(record["d0_gain_pct"]),
                "D0最高价": float(record["signal_high"]),
                "成交量放大": "是" if bool(record["volume_expanded"]) else "否",
                "买入价": buy_price,
                "止损价": float(exit_result["stop_price"]),
                "止盈价": float(exit_result["target_price"]),
                "卖出日": sell_date.strftime("%Y-%m-%d"),
                "卖出价": sell_price,
                "卖出原因": exit_result["sell_reason"],
                "持有交易日": holding_days,
                "收益率%": float(exit_result["return_pct"]),
            }
        )

    return pd.DataFrame(trades)


def summary_frame(trades: pd.DataFrame, args: argparse.Namespace) -> pd.DataFrame:
    metrics = metrics_for_trades(trades)
    rows = [
        ("start_date", args.start_date),
        ("end_date", args.end_date),
        ("key_level", args.key_level),
        ("trend_state", args.trend_state),
        ("close_range", f"[{args.close_min}, {args.close_max})"),
        ("gain_range_pct", f"[{args.gain_min}, {args.gain_max})"),
        ("max_signal_high", args.max_signal_high),
        ("volume_filter", "not_required"),
        ("ranking", "score desc, D0 close desc, code asc"),
        ("entry", "next trading day open"),
        ("stop_loss_pct", args.stop_pct * 100.0),
        ("take_profit_pct", args.target_pct * 100.0),
        ("max_hold_days", args.max_hold_days),
        ("event_mode", "independent daily top1; no account position conflict"),
        ("trade_count", int(metrics["trades"])),
        ("win_rate_pct", metrics["win_rate"]),
        ("avg_return_pct", metrics["avg_return"]),
        ("median_return_pct", metrics["median_return"]),
        ("compound_return_pct", metrics["cum_return"]),
        ("max_single_loss_pct", metrics["max_loss"]),
        ("max_single_gain_pct", metrics["max_gain"]),
    ]
    return pd.DataFrame(rows, columns=["项目", "值"])


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Backtest independent daily Top1 events for the 100 yuan key-level strategy."
    )
    parser.add_argument("--signals-file", type=Path, default=DEFAULT_SIGNALS_FILE)
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    parser.add_argument("--prefix", default=DEFAULT_PREFIX)
    parser.add_argument("--start-date", default="2025-01-01")
    parser.add_argument("--end-date", default="2025-12-31")
    parser.add_argument("--key-level", type=float, default=100.0)
    parser.add_argument("--trend-state", default="up")
    parser.add_argument("--close-min", type=float, default=90.0)
    parser.add_argument("--close-max", type=float, default=95.0)
    parser.add_argument("--gain-min", type=float, default=7.0)
    parser.add_argument("--gain-max", type=float, default=20.0)
    parser.add_argument("--max-signal-high", type=float, default=100.0)
    parser.add_argument("--preferred-close-min", type=float, default=92.5)
    parser.add_argument("--stop-pct", type=float, default=0.08)
    parser.add_argument("--target-pct", type=float, default=0.15)
    parser.add_argument("--max-hold-days", type=int, default=15)
    parser.add_argument("--max-bad-gap", type=float, default=0.35)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    args.train_start = pd.Timestamp(args.start_date)
    args.valid_end = pd.Timestamp(args.end_date)
    args.test_end = None
    signals = load_signals(args.signals_file)
    records = build_sweep_records(signals, args)
    params = build_params(args)
    trades = independent_event_trades(
        records=records,
        params=params,
        start_date=pd.Timestamp(args.start_date),
        end_date=pd.Timestamp(args.end_date),
    )
    summary = summary_frame(trades, args)

    args.out_dir.mkdir(parents=True, exist_ok=True)
    trades_file = args.out_dir / f"{args.prefix}_trades.csv"
    summary_file = args.out_dir / f"{args.prefix}_summary.csv"
    trades.to_csv(trades_file, index=False, encoding="utf-8-sig", float_format="%.4f")
    summary.to_csv(summary_file, index=False, encoding="utf-8-sig", float_format="%.4f")

    print("策略: 100元关键价位；趋势=up；D0收盘90-95；D0涨幅7%-20%；D0最高价<100；不强制放量")
    print("口径: 独立事件；每天按现有评分选Top1；下一交易日开盘买；止损8%；止盈15%；最长15个交易日")
    print(summary.to_string(index=False))
    if trades.empty:
        print("明细: 无交易")
    else:
        preview_columns = ["信号日", "股票代码", "股票名称", "买入日", "买入价", "卖出日", "卖出价", "卖出原因", "收益率%"]
        print()
        print(trades[preview_columns].to_string(index=False))
    print()
    print(f"明细: {trades_file.resolve()}")
    print(f"汇总: {summary_file.resolve()}")


if __name__ == "__main__":
    main()
