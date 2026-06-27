from __future__ import annotations

import argparse
from itertools import product
from pathlib import Path

import pandas as pd

from backtest_key_level_trailing_stop import (
    DEFAULT_OUT_DIR,
    DEFAULT_SIGNALS_FILE,
    fixed_exit_from_future,
    has_obvious_bad_window,
    load_price_frame,
    load_signals,
    normalize_code,
    select_daily_records,
)


CLOSE_RANGES = (
    (85.0, 90.0),
    (85.0, 92.0),
    (88.0, 92.0),
    (88.0, 95.0),
    (90.0, 93.0),
    (90.0, 95.0),
    (92.0, 95.0),
    (92.0, 98.0),
    (95.0, 100.0),
)
GAIN_RANGES = (
    (3.0, 10.0),
    (3.0, 15.0),
    (5.0, 12.0),
    (5.0, 15.0),
    (7.0, 15.0),
    (7.0, 20.0),
    (10.0, 20.0),
)
MAX_SIGNAL_HIGHS = (96.0, 98.0, 100.0)
VOLUME_ONLY = (False, True)
STOP_PCTS = (0.03, 0.04, 0.05, 0.06, 0.08, 0.10)
TARGET_PCTS = (0.06, 0.08, 0.10, 0.12, 0.15, 0.20)
MAX_HOLD_DAYS = (5, 10, 15, 20)


def build_records(signals: pd.DataFrame, args: argparse.Namespace) -> list[dict[str, object]]:
    base = signals[
        signals["日期"].between(args.start_date, args.test_end)
        & signals["关键价位"].eq(args.key_level)
        & signals["趋势状态"].eq(args.trend_state)
        & signals["收盘价"].ge(min(low for low, _ in CLOSE_RANGES))
        & signals["收盘价"].lt(max(high for _, high in CLOSE_RANGES))
    ].copy()

    records: list[dict[str, object]] = []
    price_cache: dict[str, pd.DataFrame | None] = {}
    max_hold_days = max(MAX_HOLD_DAYS)

    for _, signal in base.iterrows():
        code = normalize_code(signal["股票代码"])
        if code not in price_cache:
            price_cache[code] = load_price_frame(code)
        df = price_cache[code]
        if df is None or df.empty:
            continue

        signal_date = pd.Timestamp(signal["日期"])
        future_idx = df.index[df["date"] > signal_date]
        if len(future_idx) == 0:
            continue

        buy_idx = int(future_idx[0])
        max_end_idx = min(buy_idx + max_hold_days - 1, len(df) - 1)
        if has_obvious_bad_window(df, buy_idx, max_end_idx, args.max_bad_gap):
            continue

        future = df.iloc[buy_idx: max_end_idx + 1].loc[:, ["date", "open", "high", "low", "close"]].copy()
        if future.empty:
            continue

        records.append(
            {
                "record_id": len(records),
                "signal_date": signal_date,
                "code": code,
                "name": signal["股票名称"],
                "d0_close": float(signal["收盘价"]),
                "d0_gain_pct": float(signal["D0涨幅%"]),
                "signal_high": float(signal["最高价"]),
                "volume_expanded": bool(signal["成交量放大_bool"]),
                "buy_date": pd.Timestamp(future.iloc[0]["date"]),
                "buy_price": float(future.iloc[0]["open"]),
                "future": future,
            }
        )

    return records


def parameter_grid(args: argparse.Namespace) -> list[dict[str, object]]:
    if args.smoke:
        close_ranges = ((90.0, 95.0), (92.0, 95.0))
        gain_ranges = ((7.0, 15.0), (7.0, 20.0))
        max_signal_highs = (98.0,)
        volume_only = (False,)
        stop_pcts = (0.04, 0.08)
        target_pcts = (0.12, 0.20)
        max_hold_days = (10, 20)
    else:
        close_ranges = CLOSE_RANGES
        gain_ranges = GAIN_RANGES
        max_signal_highs = MAX_SIGNAL_HIGHS
        volume_only = VOLUME_ONLY
        stop_pcts = STOP_PCTS
        target_pcts = TARGET_PCTS
        max_hold_days = MAX_HOLD_DAYS

    params: list[dict[str, object]] = []
    for close_range, gain_range, max_high, vol_only, stop, target, hold in product(
        close_ranges,
        gain_ranges,
        max_signal_highs,
        volume_only,
        stop_pcts,
        target_pcts,
        max_hold_days,
    ):
        params.append(
            {
                "close_range": close_range,
                "gain_range": gain_range,
                "max_signal_high": max_high,
                "volume_only": vol_only,
                "stop_pct": stop,
                "target_pct": target,
                "max_hold_days": hold,
                "preferred_close_min": args.preferred_close_min,
            }
        )
    return params


def signal_key(params: dict[str, object]) -> tuple[object, ...]:
    return (
        params["close_range"],
        params["gain_range"],
        float(params["max_signal_high"]),
        bool(params["volume_only"]),
    )


def exit_key(params: dict[str, object]) -> tuple[object, ...]:
    return (
        float(params["stop_pct"]),
        float(params["target_pct"]),
        int(params["max_hold_days"]),
    )


def params_label(params: dict[str, object]) -> dict[str, object]:
    close_min, close_max = params["close_range"]
    gain_min, gain_max = params["gain_range"]
    stop_pct = float(params["stop_pct"])
    target_pct = float(params["target_pct"])
    return {
        "收盘价下限": close_min,
        "收盘价上限": close_max,
        "D0涨幅下限": gain_min,
        "D0涨幅上限": gain_max,
        "D0最高价上限": float(params["max_signal_high"]),
        "只选放量": bool(params["volume_only"]),
        "止损%": stop_pct * 100.0,
        "止盈%": target_pct * 100.0,
        "收益风险比": target_pct / stop_pct if stop_pct > 0 else 0.0,
        "最长持有": int(params["max_hold_days"]),
    }


def should_skip_record(record: dict[str, object], args: argparse.Namespace) -> tuple[bool, str]:
    signal_date = pd.Timestamp(record["signal_date"])
    code = normalize_code(record["code"])
    if args.exclude_bad_ltg and code == "688721" and signal_date == pd.Timestamp("2026-06-09"):
        return True, "排除龙图光罩异常交易"

    open_gap_pct = (float(record["buy_price"]) / float(record["d0_close"]) - 1.0) * 100.0
    if open_gap_pct <= args.min_next_open_to_signal_close_pct:
        return True, f"次日开盘低于D0收盘超过阈值({open_gap_pct:.2f}%)"
    return False, ""


def selected_records(
    records: list[dict[str, object]],
    params: dict[str, object],
    start_date: pd.Timestamp,
    end_date: pd.Timestamp,
    cache: dict[tuple[tuple[object, ...], pd.Timestamp, pd.Timestamp], list[dict[str, object]]],
) -> list[dict[str, object]]:
    key = (signal_key(params), start_date, end_date)
    if key not in cache:
        cache[key] = select_daily_records(records, params, start_date, end_date)
    return cache[key]


def cached_exit(
    record: dict[str, object],
    params: dict[str, object],
    cache: dict[tuple[int, tuple[object, ...]], dict[str, object]],
) -> dict[str, object]:
    key = (int(record["record_id"]), exit_key(params))
    if key not in cache:
        cache[key] = fixed_exit_from_future(record, params)
    return cache[key]


def trade_row(record: dict[str, object], params: dict[str, object], exit_result: dict[str, object]) -> dict[str, object]:
    buy_date = pd.Timestamp(record["buy_date"])
    sell_date = pd.Timestamp(exit_result["sell_date"])
    future = record["future"].head(int(params["max_hold_days"]))
    sell_positions = future.index[future["date"].eq(sell_date)]
    holding_days = int(sell_positions[0] - future.index[0] + 1) if len(sell_positions) else pd.NA
    open_gap_pct = (float(record["buy_price"]) / float(record["d0_close"]) - 1.0) * 100.0
    return {
        "信号日": pd.Timestamp(record["signal_date"]).strftime("%Y-%m-%d"),
        "买入日": buy_date.strftime("%Y-%m-%d"),
        "股票代码": normalize_code(record["code"]),
        "股票名称": record["name"],
        "D0收盘": float(record["d0_close"]),
        "D0涨幅%": float(record["d0_gain_pct"]),
        "D0最高价": float(record["signal_high"]),
        "次日开盘": float(record["buy_price"]),
        "开盘相对D0收盘%": open_gap_pct,
        "买入价": float(record["buy_price"]),
        "止损价": float(exit_result["stop_price"]),
        "止盈价": float(exit_result["target_price"]),
        "卖出日": sell_date.strftime("%Y-%m-%d"),
        "卖出价": float(exit_result["sell_price"]),
        "卖出原因": exit_result["sell_reason"],
        "持有交易日": holding_days,
        "收益率%": float(exit_result["return_pct"]),
    }


def trades_for_period(
    records: list[dict[str, object]],
    params: dict[str, object],
    start_date: pd.Timestamp,
    end_date: pd.Timestamp,
    args: argparse.Namespace,
    select_cache: dict[tuple[tuple[object, ...], pd.Timestamp, pd.Timestamp], list[dict[str, object]]],
    exit_cache: dict[tuple[int, tuple[object, ...]], dict[str, object]],
    single_position: bool,
) -> pd.DataFrame:
    selected = selected_records(records, params, start_date, end_date, select_cache)
    rows: list[dict[str, object]] = []
    position_until = pd.Timestamp.min

    for record in selected:
        signal_date = pd.Timestamp(record["signal_date"])
        if single_position and signal_date <= position_until:
            continue
        skip, _ = should_skip_record(record, args)
        if skip:
            continue

        exit_result = cached_exit(record, params, exit_cache)
        row = trade_row(record, params, exit_result)
        rows.append(row)
        if single_position:
            position_until = pd.Timestamp(row["卖出日"])

    out = pd.DataFrame(rows)
    if not out.empty:
        equity = 1.0
        cumulative: list[float] = []
        for value in out["收益率%"].astype(float):
            equity *= 1.0 + value / 100.0
            cumulative.append((equity - 1.0) * 100.0)
        out["累计收益%"] = cumulative
    return out


def returns_for_period(
    records: list[dict[str, object]],
    params: dict[str, object],
    start_date: pd.Timestamp,
    end_date: pd.Timestamp,
    args: argparse.Namespace,
    select_cache: dict[tuple[tuple[object, ...], pd.Timestamp, pd.Timestamp], list[dict[str, object]]],
    exit_cache: dict[tuple[int, tuple[object, ...]], dict[str, object]],
    single_position: bool,
) -> list[float]:
    selected = selected_records(records, params, start_date, end_date, select_cache)
    returns: list[float] = []
    position_until = pd.Timestamp.min

    for record in selected:
        signal_date = pd.Timestamp(record["signal_date"])
        if single_position and signal_date <= position_until:
            continue
        skip, _ = should_skip_record(record, args)
        if skip:
            continue

        exit_result = cached_exit(record, params, exit_cache)
        returns.append(float(exit_result["return_pct"]))
        if single_position:
            position_until = pd.Timestamp(exit_result["sell_date"])

    return returns


def metrics(trades: pd.DataFrame) -> dict[str, float]:
    if trades.empty:
        return {
            "trade_count": 0.0,
            "win_rate_pct": 0.0,
            "avg_return_pct": 0.0,
            "median_return_pct": 0.0,
            "compound_return_pct": 0.0,
            "max_single_loss_pct": 0.0,
            "max_single_gain_pct": 0.0,
            "payoff_ratio": 0.0,
            "expectancy_pct": 0.0,
        }

    returns = trades["收益率%"].astype(float)
    equity = 1.0
    for value in returns:
        equity *= 1.0 + value / 100.0
    wins = returns[returns > 0.0]
    losses = returns[returns <= 0.0]
    avg_win = float(wins.mean()) if len(wins) else 0.0
    avg_loss_abs = abs(float(losses.mean())) if len(losses) else 0.0
    return {
        "trade_count": float(len(returns)),
        "win_rate_pct": float((returns > 0.0).mean() * 100.0),
        "avg_return_pct": float(returns.mean()),
        "median_return_pct": float(returns.median()),
        "compound_return_pct": float((equity - 1.0) * 100.0),
        "max_single_loss_pct": float(returns.min()),
        "max_single_gain_pct": float(returns.max()),
        "payoff_ratio": avg_win / avg_loss_abs if avg_loss_abs > 0.0 else 0.0,
        "expectancy_pct": float(returns.mean()),
    }


def metrics_from_returns(returns_list: list[float]) -> dict[str, float]:
    if not returns_list:
        return {
            "trade_count": 0.0,
            "win_rate_pct": 0.0,
            "avg_return_pct": 0.0,
            "median_return_pct": 0.0,
            "compound_return_pct": 0.0,
            "max_single_loss_pct": 0.0,
            "max_single_gain_pct": 0.0,
            "payoff_ratio": 0.0,
            "expectancy_pct": 0.0,
        }

    returns = pd.Series(returns_list, dtype=float)
    equity = 1.0
    for value in returns:
        equity *= 1.0 + float(value) / 100.0
    wins = returns[returns > 0.0]
    losses = returns[returns <= 0.0]
    avg_win = float(wins.mean()) if len(wins) else 0.0
    avg_loss_abs = abs(float(losses.mean())) if len(losses) else 0.0
    return {
        "trade_count": float(len(returns)),
        "win_rate_pct": float((returns > 0.0).mean() * 100.0),
        "avg_return_pct": float(returns.mean()),
        "median_return_pct": float(returns.median()),
        "compound_return_pct": float((equity - 1.0) * 100.0),
        "max_single_loss_pct": float(returns.min()),
        "max_single_gain_pct": float(returns.max()),
        "payoff_ratio": avg_win / avg_loss_abs if avg_loss_abs > 0.0 else 0.0,
        "expectancy_pct": float(returns.mean()),
    }


def add_metrics(row: dict[str, object], prefix: str, trades: pd.DataFrame) -> None:
    for key, value in metrics(trades).items():
        row[f"{prefix}_{key}"] = value


def evaluate_params(
    records: list[dict[str, object]],
    params: dict[str, object],
    args: argparse.Namespace,
    select_cache: dict[tuple[tuple[object, ...], pd.Timestamp, pd.Timestamp], list[dict[str, object]]],
    exit_cache: dict[tuple[int, tuple[object, ...]], dict[str, object]],
) -> dict[str, object]:
    row = params_label(params)
    periods = {
        "2024": (pd.Timestamp("2024-01-01"), pd.Timestamp("2024-12-31")),
        "2025": (pd.Timestamp("2025-01-01"), pd.Timestamp("2025-12-31")),
        "2026": (pd.Timestamp("2026-01-01"), args.test_end),
    }
    for year, (start_date, end_date) in periods.items():
        independent_returns = returns_for_period(records, params, start_date, end_date, args, select_cache, exit_cache, False)
        single_returns = returns_for_period(records, params, start_date, end_date, args, select_cache, exit_cache, True)
        for key, value in metrics_from_returns(independent_returns).items():
            row[f"{year}_independent_{key}"] = value
        for key, value in metrics_from_returns(single_returns).items():
            row[f"{year}_single_{key}"] = value

    row["passes_train_filter"] = (
        row["2024_single_trade_count"] >= args.min_year_trades
        and row["2025_single_trade_count"] >= args.min_year_trades
        and row["2024_single_win_rate_pct"] >= args.min_year_win_rate
        and row["2025_single_win_rate_pct"] >= args.min_year_win_rate
    )
    row["train_min_win_rate_pct"] = min(row["2024_single_win_rate_pct"], row["2025_single_win_rate_pct"])
    row["train_min_avg_return_pct"] = min(row["2024_single_avg_return_pct"], row["2025_single_avg_return_pct"])
    row["train_combined_compound_pct"] = (
        (1.0 + row["2024_single_compound_return_pct"] / 100.0)
        * (1.0 + row["2025_single_compound_return_pct"] / 100.0)
        - 1.0
    ) * 100.0
    return row


def params_from_row(row: pd.Series, preferred_close_min: float) -> dict[str, object]:
    return {
        "close_range": (float(row["收盘价下限"]), float(row["收盘价上限"])),
        "gain_range": (float(row["D0涨幅下限"]), float(row["D0涨幅上限"])),
        "max_signal_high": float(row["D0最高价上限"]),
        "volume_only": bool(row["只选放量"]),
        "stop_pct": float(row["止损%"]) / 100.0,
        "target_pct": float(row["止盈%"]) / 100.0,
        "max_hold_days": int(row["最长持有"]),
        "preferred_close_min": preferred_close_min,
    }


def previous_params(preferred_close_min: float) -> list[tuple[str, dict[str, object]]]:
    return [
        (
            "previous_stop10_target20",
            {
                "close_range": (92.0, 95.0),
                "gain_range": (7.0, 15.0),
                "max_signal_high": 98.0,
                "volume_only": False,
                "stop_pct": 0.10,
                "target_pct": 0.20,
                "max_hold_days": 20,
                "preferred_close_min": preferred_close_min,
            },
        ),
        (
            "previous_stop4_target20",
            {
                "close_range": (92.0, 95.0),
                "gain_range": (7.0, 15.0),
                "max_signal_high": 98.0,
                "volume_only": False,
                "stop_pct": 0.04,
                "target_pct": 0.20,
                "max_hold_days": 20,
                "preferred_close_min": preferred_close_min,
            },
        ),
    ]


def write_best_trades(
    records: list[dict[str, object]],
    params: dict[str, object],
    args: argparse.Namespace,
    select_cache: dict[tuple[tuple[object, ...], pd.Timestamp, pd.Timestamp], list[dict[str, object]]],
    exit_cache: dict[tuple[int, tuple[object, ...]], dict[str, object]],
) -> None:
    for year, start_date, end_date in [
        ("2024", pd.Timestamp("2024-01-01"), pd.Timestamp("2024-12-31")),
        ("2025", pd.Timestamp("2025-01-01"), pd.Timestamp("2025-12-31")),
        ("2026", pd.Timestamp("2026-01-01"), args.test_end),
    ]:
        trades = trades_for_period(records, params, start_date, end_date, args, select_cache, exit_cache, True)
        out_file = args.out_dir / f"{args.prefix}_best_{year}_single_trades.csv"
        trades.to_csv(out_file, index=False, encoding="utf-8-sig", float_format="%.4f")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Find key-level rules with stable 2024/2025 win rates and validate 2026.")
    parser.add_argument("--signals-file", type=Path, default=DEFAULT_SIGNALS_FILE)
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    parser.add_argument("--prefix", default="sweep_key_level_100_multi_year_stable")
    parser.add_argument("--key-level", type=float, default=100.0)
    parser.add_argument("--trend-state", default="up")
    parser.add_argument("--start-date", type=pd.Timestamp, default=pd.Timestamp("2024-01-01"))
    parser.add_argument("--test-end", type=pd.Timestamp, default=None)
    parser.add_argument("--min-year-trades", type=int, default=8)
    parser.add_argument("--min-year-win-rate", type=float, default=60.0)
    parser.add_argument("--min-next-open-to-signal-close-pct", type=float, default=-2.0)
    parser.add_argument("--exclude-bad-ltg", action="store_true", default=True)
    parser.add_argument("--preferred-close-min", type=float, default=92.5)
    parser.add_argument("--max-bad-gap", type=float, default=0.35)
    parser.add_argument("--top-n", type=int, default=30)
    parser.add_argument("--smoke", action="store_true")
    parser.add_argument("--progress", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    signals = load_signals(args.signals_file)
    if args.test_end is None:
        args.test_end = pd.Timestamp(signals["日期"].max())

    args.out_dir.mkdir(parents=True, exist_ok=True)
    records = build_records(signals, args)
    params_list = parameter_grid(args)
    select_cache: dict[tuple[tuple[object, ...], pd.Timestamp, pd.Timestamp], list[dict[str, object]]] = {}
    exit_cache: dict[tuple[int, tuple[object, ...]], dict[str, object]] = {}

    rows: list[dict[str, object]] = []
    for idx, params in enumerate(params_list, start=1):
        if args.progress and (idx == 1 or idx % 500 == 0 or idx == len(params_list)):
            print(f"扫描进度: {idx}/{len(params_list)}")
        row = evaluate_params(records, params, args, select_cache, exit_cache)
        if row["passes_train_filter"]:
            rows.append(row)

    ranked = pd.DataFrame(rows)
    if not ranked.empty:
        ranked = ranked.sort_values(
            [
                "train_min_win_rate_pct",
                "train_min_avg_return_pct",
                "train_combined_compound_pct",
                "2026_single_win_rate_pct",
            ],
            ascending=[False, False, False, False],
        ).reset_index(drop=True)

    ranked_file = args.out_dir / f"{args.prefix}_ranked.csv"
    ranked.head(args.top_n).to_csv(ranked_file, index=False, encoding="utf-8-sig", float_format="%.4f")

    compare_rows: list[dict[str, object]] = []
    if not ranked.empty:
        best_params = params_from_row(ranked.iloc[0], args.preferred_close_min)
        write_best_trades(records, best_params, args, select_cache, exit_cache)
        best_row = evaluate_params(records, best_params, args, select_cache, exit_cache)
        best_row = {"规则": "best_multi_year_stable", **best_row}
        compare_rows.append(best_row)

    for name, params in previous_params(args.preferred_close_min):
        row = evaluate_params(records, params, args, select_cache, exit_cache)
        compare_rows.append({"规则": name, **row})

    compare_file = args.out_dir / f"{args.prefix}_compare.csv"
    pd.DataFrame(compare_rows).to_csv(compare_file, index=False, encoding="utf-8-sig", float_format="%.4f")

    print(f"候选事件缓存数: {len(records)}")
    print(f"扫描参数组合数: {len(params_list)}")
    print(
        f"满足训练过滤的规则数: {len(ranked)} "
        f"(2024/2025 单持仓交易数>={args.min_year_trades}, 胜率>={args.min_year_win_rate:.1f}%)"
    )
    print(f"排名: {ranked_file.resolve()}")
    print(f"对比: {compare_file.resolve()}")
    if ranked.empty:
        print("没有满足多年份胜率和样本门槛的规则。")
        return
    print("最佳规则:")
    print(ranked.head(1).to_string(index=False))
    print(f"最佳规则2024单持仓明细: {(args.out_dir / (args.prefix + '_best_2024_single_trades.csv')).resolve()}")
    print(f"最佳规则2025单持仓明细: {(args.out_dir / (args.prefix + '_best_2025_single_trades.csv')).resolve()}")
    print(f"最佳规则2026单持仓明细: {(args.out_dir / (args.prefix + '_best_2026_single_trades.csv')).resolve()}")


if __name__ == "__main__":
    main()
