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
TARGET_PCTS = (0.04, 0.06, 0.08, 0.10, 0.12, 0.15, 0.20)
MAX_HOLD_DAYS = (3, 5, 10, 15, 20)


def build_records(signals: pd.DataFrame, args: argparse.Namespace) -> list[dict[str, object]]:
    base = signals[
        signals["日期"].between(args.valid_start, args.test_end)
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
        gain_ranges = ((7.0, 20.0),)
        max_signal_highs = (98.0, 100.0)
        volume_only = (False,)
        stop_pcts = (0.06, 0.08)
        target_pcts = (0.12, 0.15)
        max_hold_days = (10, 15)
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


def event_trades(
    records: list[dict[str, object]],
    params: dict[str, object],
    start_date: pd.Timestamp,
    end_date: pd.Timestamp,
) -> pd.DataFrame:
    selected = select_daily_records(records, params, start_date, end_date)
    rows: list[dict[str, object]] = []

    for record in selected:
        exit_result = fixed_exit_from_future(record, params)
        sell_date = pd.Timestamp(exit_result["sell_date"])
        buy_date = pd.Timestamp(record["buy_date"])
        future = record["future"].head(int(params["max_hold_days"]))
        sell_positions = future.index[future["date"].eq(sell_date)]
        holding_days = int(sell_positions[0] - future.index[0] + 1) if len(sell_positions) else pd.NA

        rows.append(
            {
                "信号日": pd.Timestamp(record["signal_date"]).strftime("%Y-%m-%d"),
                "买入日": buy_date.strftime("%Y-%m-%d"),
                "股票代码": normalize_code(record["code"]),
                "股票名称": record["name"],
                "评分": int(record["score"]),
                "D0收盘": float(record["d0_close"]),
                "D0涨幅%": float(record["d0_gain_pct"]),
                "D0最高价": float(record["signal_high"]),
                "成交量放大": "是" if bool(record["volume_expanded"]) else "否",
                "买入价": float(record["buy_price"]),
                "止损价": float(exit_result["stop_price"]),
                "止盈价": float(exit_result["target_price"]),
                "卖出日": sell_date.strftime("%Y-%m-%d"),
                "卖出价": float(exit_result["sell_price"]),
                "卖出原因": exit_result["sell_reason"],
                "持有交易日": holding_days,
                "收益率%": float(exit_result["return_pct"]),
            }
        )

    return pd.DataFrame(rows)


def wilson_lower_bound(wins: int, n: int, z: float = 1.96) -> float:
    if n <= 0:
        return 0.0
    p = wins / n
    denom = 1.0 + z * z / n
    centre = p + z * z / (2.0 * n)
    margin = z * ((p * (1.0 - p) + z * z / (4.0 * n)) / n) ** 0.5
    return (centre - margin) / denom


def trade_metrics(trades: pd.DataFrame) -> dict[str, float]:
    if trades.empty:
        return {
            "sample_count": 0.0,
            "win_rate_pct": 0.0,
            "wilson_lower_95_pct": 0.0,
            "avg_return_pct": 0.0,
            "median_return_pct": 0.0,
            "compound_return_pct": 0.0,
            "max_single_loss_pct": 0.0,
            "max_single_gain_pct": 0.0,
            "payoff_ratio": 0.0,
            "expectancy_pct": 0.0,
        }

    returns = trades["收益率%"].astype(float)
    wins = returns[returns > 0.0]
    losses = returns[returns <= 0.0]
    equity = 1.0
    for value in returns:
        equity *= 1.0 + value / 100.0

    win_count = int((returns > 0.0).sum())
    sample_count = int(len(returns))
    avg_win = float(wins.mean()) if len(wins) else 0.0
    avg_loss_abs = abs(float(losses.mean())) if len(losses) else 0.0
    payoff_ratio = avg_win / avg_loss_abs if avg_loss_abs > 0.0 else 0.0

    return {
        "sample_count": float(sample_count),
        "win_rate_pct": win_count / sample_count * 100.0,
        "wilson_lower_95_pct": wilson_lower_bound(win_count, sample_count) * 100.0,
        "avg_return_pct": float(returns.mean()),
        "median_return_pct": float(returns.median()),
        "compound_return_pct": (equity - 1.0) * 100.0,
        "max_single_loss_pct": float(returns.min()),
        "max_single_gain_pct": float(returns.max()),
        "payoff_ratio": payoff_ratio,
        "expectancy_pct": float(returns.mean()),
    }


def params_label(params: dict[str, object]) -> dict[str, object]:
    close_min, close_max = params["close_range"]
    gain_min, gain_max = params["gain_range"]
    stop_pct = float(params["stop_pct"])
    target_pct = float(params["target_pct"])
    risk_reward_ratio = target_pct / stop_pct if stop_pct > 0.0 else 0.0
    return {
        "收盘价下限": close_min,
        "收盘价上限": close_max,
        "D0涨幅下限": gain_min,
        "D0涨幅上限": gain_max,
        "D0最高价上限": float(params["max_signal_high"]),
        "只选放量": bool(params["volume_only"]),
        "止损%": stop_pct * 100.0,
        "止盈%": target_pct * 100.0,
        "收益风险比": risk_reward_ratio,
        "最长持有": int(params["max_hold_days"]),
    }


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


def cached_exit(
    record: dict[str, object],
    params: dict[str, object],
    cache: dict[tuple[int, tuple[object, ...]], dict[str, object]],
) -> dict[str, object]:
    key = (int(record["record_id"]), exit_key(params))
    if key not in cache:
        cache[key] = fixed_exit_from_future(record, params)
    return cache[key]


def metrics_from_records(
    selected: list[dict[str, object]],
    params: dict[str, object],
    exit_cache: dict[tuple[int, tuple[object, ...]], dict[str, object]],
) -> dict[str, float]:
    if not selected:
        return trade_metrics(pd.DataFrame())
    returns = [
        float(cached_exit(record, params, exit_cache)["return_pct"])
        for record in selected
    ]
    return trade_metrics(pd.DataFrame({"收益率%": returns}))


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


def evaluate(
    records: list[dict[str, object]],
    params: dict[str, object],
    args: argparse.Namespace,
    select_cache: dict[tuple[tuple[object, ...], pd.Timestamp, pd.Timestamp], list[dict[str, object]]],
    exit_cache: dict[tuple[int, tuple[object, ...]], dict[str, object]],
) -> dict[str, object]:
    valid_selected = selected_records(records, params, args.valid_start, args.valid_end, select_cache)
    valid_metrics = metrics_from_records(valid_selected, params, exit_cache)
    row = params_label(params)
    row.update({f"2025_{key}": value for key, value in valid_metrics.items()})
    row["passes_constraints"] = (
        row["收益风险比"] >= args.min_risk_reward
        and valid_metrics["sample_count"] >= args.min_sample
        and valid_metrics["win_rate_pct"] >= args.min_win_rate
    )

    if valid_metrics["sample_count"] >= args.min_sample:
        test_selected = selected_records(records, params, args.test_start, args.test_end, select_cache)
        test_metrics = metrics_from_records(test_selected, params, exit_cache)
    else:
        test_metrics = trade_metrics(pd.DataFrame())
    row.update({f"2026_{key}": value for key, value in test_metrics.items()})
    return row


def params_from_row(row: pd.Series) -> dict[str, object]:
    return {
        "close_range": (float(row["收盘价下限"]), float(row["收盘价上限"])),
        "gain_range": (float(row["D0涨幅下限"]), float(row["D0涨幅上限"])),
        "max_signal_high": float(row["D0最高价上限"]),
        "volume_only": bool(row["只选放量"]),
        "stop_pct": float(row["止损%"]) / 100.0,
        "target_pct": float(row["止盈%"]) / 100.0,
        "max_hold_days": int(row["最长持有"]),
        "preferred_close_min": 92.5,
    }


def pure_winrate_params(preferred_close_min: float) -> dict[str, object]:
    return {
        "close_range": (92.0, 98.0),
        "gain_range": (5.0, 12.0),
        "max_signal_high": 96.0,
        "volume_only": True,
        "stop_pct": 0.10,
        "target_pct": 0.04,
        "max_hold_days": 15,
        "preferred_close_min": preferred_close_min,
    }


def write_outputs(records: list[dict[str, object]], ranked: pd.DataFrame, args: argparse.Namespace) -> None:
    args.out_dir.mkdir(parents=True, exist_ok=True)
    ranked_file = args.out_dir / f"{args.prefix}_ranked.csv"
    compare_file = args.out_dir / f"{args.prefix}_compare.csv"
    best_2025_file = args.out_dir / f"{args.prefix}_best_2025_trades.csv"
    best_2026_file = args.out_dir / f"{args.prefix}_best_2026_trades.csv"

    ranked.head(args.top_n).to_csv(ranked_file, index=False, encoding="utf-8-sig", float_format="%.4f")

    if ranked.empty:
        pd.DataFrame().to_csv(compare_file, index=False, encoding="utf-8-sig")
        pd.DataFrame().to_csv(best_2025_file, index=False, encoding="utf-8-sig")
        pd.DataFrame().to_csv(best_2026_file, index=False, encoding="utf-8-sig")
        return

    best_params = params_from_row(ranked.iloc[0])
    best_2025 = event_trades(records, best_params, args.valid_start, args.valid_end)
    best_2026 = event_trades(records, best_params, args.test_start, args.test_end)
    best_2025.to_csv(best_2025_file, index=False, encoding="utf-8-sig", float_format="%.4f")
    best_2026.to_csv(best_2026_file, index=False, encoding="utf-8-sig", float_format="%.4f")

    current_params = {
        "close_range": (90.0, 95.0),
        "gain_range": (7.0, 20.0),
        "max_signal_high": 100.0,
        "volume_only": False,
        "stop_pct": 0.08,
        "target_pct": 0.15,
        "max_hold_days": 15,
        "preferred_close_min": args.preferred_close_min,
    }
    compare_rows: list[dict[str, object]] = []
    for name, params in [
        ("best_expectancy", best_params),
        ("previous_best_winrate_10_4_15d", pure_winrate_params(args.preferred_close_min)),
        ("current_8_15_15d", current_params),
    ]:
        row = {"规则": name}
        row.update(params_label(params))
        row.update({f"2025_{key}": value for key, value in trade_metrics(event_trades(records, params, args.valid_start, args.valid_end)).items()})
        row.update({f"2026_{key}": value for key, value in trade_metrics(event_trades(records, params, args.test_start, args.test_end)).items()})
        compare_rows.append(row)
    pd.DataFrame(compare_rows).to_csv(compare_file, index=False, encoding="utf-8-sig", float_format="%.4f")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Sweep independent daily Top1 key-level rules by constrained expectancy.")
    parser.add_argument("--signals-file", type=Path, default=DEFAULT_SIGNALS_FILE)
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    parser.add_argument("--prefix", default="sweep_key_level_100_independent_expectancy")
    parser.add_argument("--key-level", type=float, default=100.0)
    parser.add_argument("--trend-state", default="up")
    parser.add_argument("--valid-start", type=pd.Timestamp, default=pd.Timestamp("2025-01-01"))
    parser.add_argument("--valid-end", type=pd.Timestamp, default=pd.Timestamp("2025-12-31"))
    parser.add_argument("--test-start", type=pd.Timestamp, default=pd.Timestamp("2026-01-01"))
    parser.add_argument("--test-end", type=pd.Timestamp, default=None)
    parser.add_argument("--min-sample", type=int, default=50)
    parser.add_argument("--min-win-rate", type=float, default=60.0)
    parser.add_argument("--min-risk-reward", type=float, default=2.0)
    parser.add_argument("--top-n", type=int, default=20)
    parser.add_argument("--preferred-close-min", type=float, default=92.5)
    parser.add_argument("--max-bad-gap", type=float, default=0.35)
    parser.add_argument("--smoke", action="store_true")
    parser.add_argument("--progress", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    signals = load_signals(args.signals_file)
    if args.test_end is None:
        args.test_end = pd.Timestamp(signals["日期"].max())

    records = build_records(signals, args)
    params_list = parameter_grid(args)
    rows: list[dict[str, object]] = []
    select_cache: dict[tuple[tuple[object, ...], pd.Timestamp, pd.Timestamp], list[dict[str, object]]] = {}
    exit_cache: dict[tuple[int, tuple[object, ...]], dict[str, object]] = {}

    for idx, params in enumerate(params_list, start=1):
        if args.progress and (idx == 1 or idx % 500 == 0 or idx == len(params_list)):
            print(f"扫描进度: {idx}/{len(params_list)}")
        row = evaluate(records, params, args, select_cache, exit_cache)
        if row["passes_constraints"]:
            rows.append(row)

    ranked = pd.DataFrame(rows)
    if not ranked.empty:
        ranked = ranked.sort_values(
            [
                "2025_avg_return_pct",
                "2025_payoff_ratio",
                "2025_compound_return_pct",
                "2025_win_rate_pct",
                "2026_avg_return_pct",
                "2025_sample_count",
            ],
            ascending=[False, False, False, False, False, False],
        ).reset_index(drop=True)

    write_outputs(records, ranked, args)

    print(f"候选事件缓存数: {len(records)}")
    print(f"扫描参数组合数: {len(params_list)}")
    print(
        f"满足约束的规则数: {len(ranked)} "
        f"(样本>={args.min_sample}, 胜率>={args.min_win_rate:.1f}%, 收益风险比>={args.min_risk_reward:.1f})"
    )
    if ranked.empty:
        print("没有满足样本门槛的规则。")
        return
    print("最佳规则:")
    print(ranked.head(1).to_string(index=False))
    print(f"排名Top{args.top_n}: {(args.out_dir / (args.prefix + '_ranked.csv')).resolve()}")
    print(f"最佳规则2025明细: {(args.out_dir / (args.prefix + '_best_2025_trades.csv')).resolve()}")
    print(f"最佳规则2026明细: {(args.out_dir / (args.prefix + '_best_2026_trades.csv')).resolve()}")
    print(f"规则对比: {(args.out_dir / (args.prefix + '_compare.csv')).resolve()}")


if __name__ == "__main__":
    main()
