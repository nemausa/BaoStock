from __future__ import annotations

import argparse
import random
from itertools import product
from pathlib import Path

import pandas as pd


BASE_DIR = Path("a_stock_data")
PARQUET_DIR = BASE_DIR / "parquet"
DEFAULT_SIGNALS_FILE = Path("strategy_recommended_runner") / "outputs" / "livermore_key_levels" / "signals.csv"
DEFAULT_OUT_DIR = Path("strategy_recommended_runner") / "outputs" / "livermore_key_levels"
SWEEP_STOP_PCTS = (0.02, 0.03, 0.04, 0.05, 0.06, 0.08)
SWEEP_TARGET_PCTS = (0.04, 0.06, 0.08, 0.10, 0.12, 0.15, 0.20)
SWEEP_MAX_HOLD_DAYS = (3, 5, 10, 15, 20)
SWEEP_GAIN_RANGES = ((3.0, 15.0), (5.0, 15.0), (7.0, 15.0), (7.0, 20.0), (10.0, 20.0))
SWEEP_MAX_SIGNAL_HIGHS = (96.0, 98.0, 100.0)
SWEEP_CLOSE_RANGES = ((90.0, 95.0), (92.0, 95.0), (90.0, 93.0))
SWEEP_VOLUME_ONLY = (False, True)


def normalize_code(code) -> str:
    code = str(code).strip()
    if "." in code:
        code = code.split(".")[-1]
    return code.zfill(6)


def load_signals(path: Path) -> pd.DataFrame:
    signals = pd.read_csv(path, dtype={"股票代码": str}, encoding="utf-8-sig", low_memory=False)
    signals = signals.copy()
    signals["股票代码"] = signals["股票代码"].apply(normalize_code)
    signals["日期"] = pd.to_datetime(signals["日期"], errors="coerce")

    for column in ["关键价位", "收盘价", "最高价", "前收盘价"]:
        signals[column] = pd.to_numeric(signals[column], errors="coerce")

    signals["D0涨幅%"] = (signals["收盘价"] / signals["前收盘价"] - 1.0) * 100.0
    signals["成交量放大_bool"] = signals["成交量放大"].astype(str).str.lower().eq("true")
    return signals


def load_price_frame(code: str) -> pd.DataFrame | None:
    path = PARQUET_DIR / f"{normalize_code(code)}.parquet"
    if not path.exists():
        return None

    df = pd.read_parquet(path).copy()
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    for column in ["open", "high", "low", "close"]:
        df[column] = pd.to_numeric(df[column], errors="coerce")

    return (
        df.dropna(subset=["date", "open", "high", "low", "close"])
        .sort_values("date")
        .reset_index(drop=True)
    )


def has_obvious_bad_window(df: pd.DataFrame, start_idx: int, end_idx: int, max_gap: float) -> bool:
    window = df.iloc[max(0, start_idx - 1): end_idx + 1]
    for _, row in window.iterrows():
        if row["high"] < max(row["open"], row["close"], row["low"]):
            return True
        if row["low"] > min(row["open"], row["close"], row["high"]):
            return True

    for idx in range(max(1, start_idx), end_idx + 1):
        prev_close = float(df.loc[idx - 1, "close"])
        if prev_close <= 0:
            return True
        open_gap = abs(float(df.loc[idx, "open"]) / prev_close - 1.0)
        close_gap = abs(float(df.loc[idx, "close"]) / prev_close - 1.0)
        if open_gap > max_gap or close_gap > max_gap:
            return True

    return False


def build_candidates(signals: pd.DataFrame, args: argparse.Namespace) -> tuple[pd.DataFrame, pd.DataFrame]:
    base = signals[
        signals["日期"].between(args.start_date, args.end_date)
        & signals["关键价位"].eq(args.key_level)
        & signals["趋势状态"].eq(args.trend_state)
        & signals["收盘价"].ge(args.close_min)
        & signals["收盘价"].lt(args.close_max)
    ].copy()

    candidates = base[
        base["D0涨幅%"].ge(args.gain_min)
        & base["D0涨幅%"].lt(args.gain_max)
        & base["最高价"].lt(args.max_signal_high)
    ].copy()

    candidates["评分"] = 0
    candidates.loc[candidates["成交量放大_bool"], "评分"] += 2
    candidates.loc[
        candidates["D0涨幅%"].ge(args.strong_gain_min)
        & candidates["D0涨幅%"].lt(args.gain_max),
        "评分",
    ] += 2
    candidates.loc[candidates["收盘价"].ge(args.preferred_close_min), "评分"] += 1
    candidates["距关键位"] = args.key_level - candidates["收盘价"]

    daily_rows = []
    for _, group in candidates.groupby("日期"):
        daily_rows.append(
            group.sort_values(["评分", "收盘价", "股票代码"], ascending=[False, False, True]).iloc[0]
        )

    if not daily_rows:
        return base, candidates.iloc[0:0].copy()

    daily = pd.DataFrame(daily_rows).sort_values(["日期", "股票代码"]).reset_index(drop=True)
    return base, daily


def simulate_trades(daily: pd.DataFrame, args: argparse.Namespace) -> tuple[pd.DataFrame, pd.DataFrame]:
    trades = []
    skips = []
    price_cache: dict[str, pd.DataFrame | None] = {}
    position_until = pd.Timestamp.min

    for _, signal in daily.iterrows():
        signal_date = pd.Timestamp(signal["日期"])
        code = normalize_code(signal["股票代码"])
        name = signal["股票名称"]

        if signal_date <= position_until:
            skips.append(skip_row(signal_date, code, name, "单账户仍持仓"))
            continue

        if code not in price_cache:
            price_cache[code] = load_price_frame(code)
        df = price_cache[code]
        if df is None or df.empty:
            skips.append(skip_row(signal_date, code, name, "缺少行情文件"))
            continue

        future_idx = df.index[df["date"] > signal_date]
        if len(future_idx) == 0:
            skips.append(skip_row(signal_date, code, name, "无次日行情"))
            continue

        buy_idx = int(future_idx[0])
        sell_limit_idx = min(buy_idx + args.max_hold_days - 1, len(df) - 1)

        if has_obvious_bad_window(df, buy_idx, sell_limit_idx, args.max_bad_gap):
            skips.append(skip_row(signal_date, code, name, "买入/持有窗口存在明显异常价格"))
            continue

        buy_date = pd.Timestamp(df.loc[buy_idx, "date"])
        buy_price = float(df.loc[buy_idx, "open"])
        if args.exit_mode == "fixed-target-stop":
            exit_result = fixed_target_stop_exit(df, buy_idx, sell_limit_idx, buy_price, args)
        else:
            exit_result = trailing_stop_exit(df, buy_idx, sell_limit_idx, buy_price, args)

        sell_idx = exit_result["sell_idx"]
        sell_date = pd.Timestamp(df.loc[sell_idx, "date"])
        sell_price = float(exit_result["sell_price"])
        returns = sell_price / buy_price - 1.0
        trades.append(
            {
                "信号日": signal_date.strftime("%Y-%m-%d"),
                "买入日": buy_date.strftime("%Y-%m-%d"),
                "股票代码": code,
                "股票名称": name,
                "评分": int(signal["评分"]),
                "D0收盘": float(signal["收盘价"]),
                "D0涨幅%": float(signal["D0涨幅%"]),
                "成交量放大": "是" if bool(signal["成交量放大_bool"]) else "否",
                "买入价": buy_price,
                "止损价": exit_result["stop_price"],
                "止盈价": exit_result["target_price"],
                "卖出日": sell_date.strftime("%Y-%m-%d"),
                "卖出价": sell_price,
                "持有交易日": int(sell_idx - buy_idx + 1),
                "最高收盘价": exit_result["highest_close"],
                "卖出原因": exit_result["sell_reason"],
                "收益率%": returns * 100.0,
            }
        )
        position_until = sell_date

    return pd.DataFrame(trades), pd.DataFrame(skips)


def trailing_stop_exit(
    df: pd.DataFrame,
    buy_idx: int,
    sell_limit_idx: int,
    buy_price: float,
    args: argparse.Namespace,
) -> dict[str, object]:
    stop_price = buy_price * (1.0 - args.stop_pct)
    highest_close = buy_price
    sell_idx = sell_limit_idx
    sell_price = float(df.loc[sell_limit_idx, "close"])
    sell_reason = "持有满%d个交易日" % args.max_hold_days

    for idx in range(buy_idx, sell_limit_idx + 1):
        close_price = float(df.loc[idx, "close"])
        previous_highest_close = highest_close
        highest_close = max(highest_close, close_price)
        trailing_enabled = previous_highest_close > buy_price
        trailing_stop_price = previous_highest_close * (1.0 - args.trailing_drawdown)

        hit_initial_stop = close_price <= stop_price
        hit_trailing_stop = trailing_enabled and close_price <= trailing_stop_price

        if hit_initial_stop or hit_trailing_stop:
            sell_idx = idx
            sell_price = close_price
            if hit_initial_stop and hit_trailing_stop:
                sell_reason = "止损/移动止盈同时触发"
            elif hit_initial_stop:
                sell_reason = "收盘跌破买入价4%止损"
            else:
                sell_reason = "收盘较最高收盘价回撤4%止盈"
            break

    return {
        "sell_idx": sell_idx,
        "sell_price": sell_price,
        "sell_reason": sell_reason,
        "stop_price": stop_price,
        "target_price": pd.NA,
        "highest_close": highest_close,
    }


def fixed_target_stop_exit(
    df: pd.DataFrame,
    buy_idx: int,
    sell_limit_idx: int,
    buy_price: float,
    args: argparse.Namespace,
) -> dict[str, object]:
    stop_price = buy_price * (1.0 - args.stop_pct)
    target_price = buy_price * (1.0 + args.target_pct)
    highest_close = buy_price
    sell_idx = sell_limit_idx
    sell_price = float(df.loc[sell_limit_idx, "close"])
    sell_reason = "持有满%d个交易日" % args.max_hold_days

    for idx in range(buy_idx, sell_limit_idx + 1):
        open_price = float(df.loc[idx, "open"])
        high_price = float(df.loc[idx, "high"])
        low_price = float(df.loc[idx, "low"])
        close_price = float(df.loc[idx, "close"])
        highest_close = max(highest_close, close_price)

        if open_price <= stop_price:
            sell_idx = idx
            sell_price = open_price
            sell_reason = "开盘跌破买入价4%止损"
            break
        if open_price >= target_price:
            sell_idx = idx
            sell_price = open_price
            sell_reason = "开盘达到买入价10%止盈"
            break

        hit_stop = low_price <= stop_price
        hit_target = high_price >= target_price
        if not hit_stop and not hit_target:
            continue

        sell_idx = idx
        if hit_stop and hit_target:
            if close_price >= open_price:
                sell_price = target_price
                sell_reason = "同日触发止盈止损，按阳线先止盈"
            else:
                sell_price = stop_price
                sell_reason = "同日触发止盈止损，按阴线先止损"
        elif hit_target:
            sell_price = target_price
            sell_reason = "盘中达到买入价10%止盈"
        else:
            sell_price = stop_price
            sell_reason = "盘中跌破买入价4%止损"
        break

    return {
        "sell_idx": sell_idx,
        "sell_price": sell_price,
        "sell_reason": sell_reason,
        "stop_price": stop_price,
        "target_price": target_price,
        "highest_close": highest_close,
    }


def build_sweep_records(signals: pd.DataFrame, args: argparse.Namespace) -> list[dict[str, object]]:
    end_date = args.valid_end if args.valid_end is not None else signals["日期"].max()
    if getattr(args, "test_end", None) is not None:
        end_date = max(pd.Timestamp(end_date), pd.Timestamp(args.test_end))
    max_hold_days = max(SWEEP_MAX_HOLD_DAYS)
    base = signals[
        signals["日期"].between(args.train_start, end_date)
        & signals["关键价位"].eq(args.key_level)
        & signals["趋势状态"].eq(args.trend_state)
        & signals["收盘价"].ge(min(low for low, _ in SWEEP_CLOSE_RANGES))
        & signals["收盘价"].lt(max(high for _, high in SWEEP_CLOSE_RANGES))
    ].copy()

    records: list[dict[str, object]] = []
    price_cache: dict[str, pd.DataFrame | None] = {}
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


def score_record(record: dict[str, object], gain_max: float, preferred_close_min: float) -> int:
    score = 0
    if bool(record["volume_expanded"]):
        score += 2
    d0_gain_pct = float(record["d0_gain_pct"])
    if 10.0 <= d0_gain_pct < gain_max:
        score += 2
    if float(record["d0_close"]) >= preferred_close_min:
        score += 1
    return score


def select_daily_records(
    records: list[dict[str, object]],
    params: dict[str, object],
    start_date: pd.Timestamp,
    end_date: pd.Timestamp,
) -> list[dict[str, object]]:
    best_by_date: dict[pd.Timestamp, dict[str, object]] = {}
    for record in records:
        signal_date = pd.Timestamp(record["signal_date"])
        if signal_date < start_date or signal_date > end_date:
            continue
        if bool(params["volume_only"]) and not bool(record["volume_expanded"]):
            continue
        d0_close = float(record["d0_close"])
        close_min, close_max = params["close_range"]
        if d0_close < close_min or d0_close >= close_max:
            continue
        d0_gain_pct = float(record["d0_gain_pct"])
        gain_min, gain_max = params["gain_range"]
        if d0_gain_pct < gain_min or d0_gain_pct >= gain_max:
            continue
        if float(record["signal_high"]) >= float(params["max_signal_high"]):
            continue

        score = score_record(record, gain_max, float(params["preferred_close_min"]))
        current = best_by_date.get(signal_date)
        sort_key = (score, d0_close, -int(record["code"]))
        if current is None:
            selected = dict(record)
            selected["score"] = score
            selected["sort_key"] = sort_key
            best_by_date[signal_date] = selected
            continue
        if sort_key > current["sort_key"]:
            selected = dict(record)
            selected["score"] = score
            selected["sort_key"] = sort_key
            best_by_date[signal_date] = selected

    return [best_by_date[date] for date in sorted(best_by_date)]


def fixed_exit_from_future(record: dict[str, object], params: dict[str, object]) -> dict[str, object]:
    buy_price = float(record["buy_price"])
    stop_price = buy_price * (1.0 - float(params["stop_pct"]))
    target_price = buy_price * (1.0 + float(params["target_pct"]))
    max_hold_days = int(params["max_hold_days"])
    future = record["future"].head(max_hold_days)
    last = future.iloc[-1]
    sell_date = pd.Timestamp(last["date"])
    sell_price = float(last["close"])
    sell_reason = "持有满%d个交易日" % max_hold_days
    highest_close = buy_price

    for _, row in future.iterrows():
        open_price = float(row["open"])
        high_price = float(row["high"])
        low_price = float(row["low"])
        close_price = float(row["close"])
        highest_close = max(highest_close, close_price)

        if open_price <= stop_price:
            sell_date = pd.Timestamp(row["date"])
            sell_price = open_price
            sell_reason = "开盘跌破买入价止损"
            break
        if open_price >= target_price:
            sell_date = pd.Timestamp(row["date"])
            sell_price = open_price
            sell_reason = "开盘达到买入价止盈"
            break

        hit_stop = low_price <= stop_price
        hit_target = high_price >= target_price
        if not hit_stop and not hit_target:
            continue

        sell_date = pd.Timestamp(row["date"])
        if hit_stop and hit_target:
            if close_price >= open_price:
                sell_price = target_price
                sell_reason = "同日触发止盈止损，按阳线先止盈"
            else:
                sell_price = stop_price
                sell_reason = "同日触发止盈止损，按阴线先止损"
        elif hit_target:
            sell_price = target_price
            sell_reason = "盘中达到买入价止盈"
        else:
            sell_price = stop_price
            sell_reason = "盘中跌破买入价止损"
        break

    return {
        "sell_date": sell_date,
        "sell_price": sell_price,
        "sell_reason": sell_reason,
        "stop_price": stop_price,
        "target_price": target_price,
        "highest_close": highest_close,
        "return_pct": (sell_price / buy_price - 1.0) * 100.0,
    }


def simulate_sweep_records(
    records: list[dict[str, object]],
    params: dict[str, object],
    start_date: pd.Timestamp,
    end_date: pd.Timestamp,
) -> pd.DataFrame:
    selected = select_daily_records(records, params, start_date, end_date)
    position_until = pd.Timestamp.min
    trades: list[dict[str, object]] = []
    for record in selected:
        signal_date = pd.Timestamp(record["signal_date"])
        if signal_date <= position_until:
            continue

        exit_result = fixed_exit_from_future(record, params)
        sell_date = pd.Timestamp(exit_result["sell_date"])
        trades.append(
            {
                "信号日": signal_date.strftime("%Y-%m-%d"),
                "买入日": pd.Timestamp(record["buy_date"]).strftime("%Y-%m-%d"),
                "股票代码": record["code"],
                "股票名称": record["name"],
                "评分": int(record["score"]),
                "D0收盘": float(record["d0_close"]),
                "D0涨幅%": float(record["d0_gain_pct"]),
                "成交量放大": "是" if bool(record["volume_expanded"]) else "否",
                "买入价": float(record["buy_price"]),
                "止损价": float(exit_result["stop_price"]),
                "止盈价": float(exit_result["target_price"]),
                "卖出日": sell_date.strftime("%Y-%m-%d"),
                "卖出价": float(exit_result["sell_price"]),
                "最高收盘价": float(exit_result["highest_close"]),
                "卖出原因": exit_result["sell_reason"],
                "收益率%": float(exit_result["return_pct"]),
            }
        )
        position_until = sell_date

    return pd.DataFrame(trades)


def metrics_for_trades(trades: pd.DataFrame) -> dict[str, float]:
    if trades.empty:
        return {
            "trades": 0,
            "win_rate": 0.0,
            "avg_return": 0.0,
            "median_return": 0.0,
            "cum_return": 0.0,
            "max_loss": 0.0,
            "max_gain": 0.0,
        }

    returns = trades["收益率%"].astype(float)
    equity = 1.0
    for value in returns:
        equity *= 1.0 + value / 100.0

    return {
        "trades": float(len(trades)),
        "win_rate": float((returns > 0).mean() * 100.0),
        "avg_return": float(returns.mean()),
        "median_return": float(returns.median()),
        "cum_return": float((equity - 1.0) * 100.0),
        "max_loss": float(returns.min()),
        "max_gain": float(returns.max()),
    }


def rolling_start_dates(
    records: list[dict[str, object]],
    params: dict[str, object],
    start_date: pd.Timestamp,
    end_date: pd.Timestamp,
) -> list[pd.Timestamp]:
    selected = select_daily_records(records, params, start_date, end_date)
    return sorted({pd.Timestamp(record["signal_date"]) for record in selected})


def rolling_start_results(
    records: list[dict[str, object]],
    params: dict[str, object],
    start_date: pd.Timestamp,
    end_date: pd.Timestamp,
) -> pd.DataFrame:
    rows: list[dict[str, object]] = []
    for rolling_start in rolling_start_dates(records, params, start_date, end_date):
        trades = simulate_sweep_records(records, params, rolling_start, end_date)
        metrics = metrics_for_trades(trades)
        first_buy_date = ""
        first_stock = ""
        if not trades.empty:
            first = trades.iloc[0]
            first_buy_date = first["买入日"]
            first_stock = f"{first['股票代码']} {first['股票名称']}"
        rows.append(
            {
                "开始信号日": rolling_start.strftime("%Y-%m-%d"),
                "交易数": int(metrics["trades"]),
                "胜率%": metrics["win_rate"],
                "累计收益%": metrics["cum_return"],
                "平均收益%": metrics["avg_return"],
                "第一笔买入日": first_buy_date,
                "第一只股票": first_stock,
            }
        )
    return pd.DataFrame(rows)


def rolling_metrics(results: pd.DataFrame) -> dict[str, float]:
    if results.empty:
        return {
            "starts": 0,
            "mean": 0.0,
            "median": 0.0,
            "worst": 0.0,
            "p25": 0.0,
            "positive_rate": 0.0,
            "std": 0.0,
        }
    returns = results["累计收益%"].astype(float)
    return {
        "starts": float(len(results)),
        "mean": float(returns.mean()),
        "median": float(returns.median()),
        "worst": float(returns.min()),
        "p25": float(returns.quantile(0.25)),
        "positive_rate": float((returns > 0.0).mean() * 100.0),
        "std": float(returns.std(ddof=0)),
    }


def first_record_on_or_after(selected: list[dict[str, object]], start_date: pd.Timestamp, start_pos: int) -> int | None:
    for idx in range(start_pos, len(selected)):
        if pd.Timestamp(selected[idx]["signal_date"]) >= start_date:
            return idx
    return None


def add_trading_days(trading_dates: list[pd.Timestamp], date: pd.Timestamp, days: int) -> pd.Timestamp:
    for idx, trading_date in enumerate(trading_dates):
        if trading_date > date:
            target_idx = min(idx + max(days - 1, 0), len(trading_dates) - 1)
            return trading_dates[target_idx]
    return pd.Timestamp.max


def simulate_random_wait_path(
    selected: list[dict[str, object]],
    trading_dates: list[pd.Timestamp],
    start_date: pd.Timestamp,
    end_date: pd.Timestamp,
    params: dict[str, object],
    rng: random.Random,
    wait_min: int,
    wait_max: int,
    wait_unit: str,
) -> pd.DataFrame:
    trades: list[dict[str, object]] = []
    next_allowed_signal_date = start_date
    search_pos = 0

    while True:
        record_idx = first_record_on_or_after(selected, next_allowed_signal_date, search_pos)
        if record_idx is None:
            break
        record = selected[record_idx]
        signal_date = pd.Timestamp(record["signal_date"])
        if signal_date > end_date:
            break

        exit_result = fixed_exit_from_future(record, params)
        sell_date = pd.Timestamp(exit_result["sell_date"])
        if sell_date > end_date:
            break

        wait_days = rng.randint(wait_min, wait_max)
        trades.append(
            {
                "信号日": signal_date.strftime("%Y-%m-%d"),
                "买入日": pd.Timestamp(record["buy_date"]).strftime("%Y-%m-%d"),
                "股票代码": record["code"],
                "股票名称": record["name"],
                "买入价": float(record["buy_price"]),
                "卖出日": sell_date.strftime("%Y-%m-%d"),
                "卖出价": float(exit_result["sell_price"]),
                "卖出原因": exit_result["sell_reason"],
                "收益率%": float(exit_result["return_pct"]),
                "等待天数": wait_days,
                "等待单位": wait_unit,
            }
        )

        if wait_unit == "calendar":
            next_allowed_signal_date = sell_date + pd.Timedelta(days=wait_days)
        else:
            next_allowed_signal_date = add_trading_days(trading_dates, sell_date, wait_days)
        search_pos = record_idx + 1

    return pd.DataFrame(trades)


def monte_carlo_paths(
    records: list[dict[str, object]],
    params: dict[str, object],
    start_date: pd.Timestamp,
    end_date: pd.Timestamp,
    runs: int,
    wait_min: int,
    wait_max: int,
    seed: int,
    wait_unit: str = "trading",
) -> pd.DataFrame:
    selected = select_daily_records(records, params, start_date, end_date)
    trading_dates = sorted(
        {
            pd.Timestamp(row["date"])
            for record in selected
            for _, row in record["future"].iterrows()
            if start_date <= pd.Timestamp(row["date"]) <= end_date
        }
        | {pd.Timestamp(record["signal_date"]) for record in selected}
    )
    prepared: list[dict[str, object]] = []
    for record in selected:
        exit_result = fixed_exit_from_future(record, params)
        sell_date = pd.Timestamp(exit_result["sell_date"])
        if sell_date > end_date:
            continue
        prepared.append(
            {
                "signal_date": pd.Timestamp(record["signal_date"]),
                "buy_date": pd.Timestamp(record["buy_date"]).strftime("%Y-%m-%d"),
                "code": record["code"],
                "name": record["name"],
                "sell_date": sell_date,
                "return_pct": float(exit_result["return_pct"]),
            }
        )

    rows: list[dict[str, object]] = []
    for run in range(runs):
        rng = random.Random(seed + run)
        returns: list[float] = []
        first_buy_date = ""
        first_stock = ""
        next_allowed_signal_date = start_date
        search_pos = 0

        while True:
            record_idx = first_record_on_or_after(prepared, next_allowed_signal_date, search_pos)
            if record_idx is None:
                break
            record = prepared[record_idx]
            signal_date = pd.Timestamp(record["signal_date"])
            if signal_date > end_date:
                break
            sell_date = pd.Timestamp(record["sell_date"])
            if sell_date > end_date:
                break

            if not returns:
                first_buy_date = str(record["buy_date"])
                first_stock = f"{record['code']} {record['name']}"
            returns.append(float(record["return_pct"]))

            wait_days = rng.randint(wait_min, wait_max)
            if wait_unit == "calendar":
                next_allowed_signal_date = sell_date + pd.Timedelta(days=wait_days)
            else:
                next_allowed_signal_date = add_trading_days(trading_dates, sell_date, wait_days)
            search_pos = record_idx + 1

        equity = 1.0
        for value in returns:
            equity *= 1.0 + value / 100.0
        trade_count = len(returns)
        win_rate = (sum(1 for value in returns if value > 0.0) / trade_count * 100.0) if trade_count else 0.0
        avg_return = (sum(returns) / trade_count) if trade_count else 0.0
        cum_return = (equity - 1.0) * 100.0
        rows.append(
            {
                "路径编号": run + 1,
                "交易数": trade_count,
                "胜率%": win_rate,
                "累计收益%": cum_return,
                "平均收益%": avg_return,
                "第一笔买入日": first_buy_date,
                "第一只股票": first_stock,
            }
        )
    return pd.DataFrame(rows)


def monte_carlo_metrics(paths: pd.DataFrame) -> dict[str, float]:
    if paths.empty:
        return {
            "runs": 0,
            "mean": 0.0,
            "median": 0.0,
            "p05": 0.0,
            "worst": 0.0,
            "positive_rate": 0.0,
            "avg_trades": 0.0,
            "std": 0.0,
        }
    returns = paths["累计收益%"].astype(float)
    return {
        "runs": float(len(paths)),
        "mean": float(returns.mean()),
        "median": float(returns.median()),
        "p05": float(returns.quantile(0.05)),
        "worst": float(returns.min()),
        "positive_rate": float((returns > 0.0).mean() * 100.0),
        "avg_trades": float(paths["交易数"].astype(float).mean()),
        "std": float(returns.std(ddof=0)),
    }


def sweep_parameter_grid(args: argparse.Namespace) -> list[dict[str, object]]:
    if args.smoke_sweep:
        stop_pcts = (0.03, 0.04)
        target_pcts = (0.08, 0.10)
        max_hold_days = (5, 10)
        gain_ranges = ((7.0, 15.0),)
        max_signal_highs = (98.0,)
        close_ranges = ((90.0, 95.0),)
        volume_only_values = (False,)
    elif args.calendar_mc_grid:
        stop_pcts = (0.04, 0.05, 0.06, 0.08)
        target_pcts = (0.10, 0.12, 0.15, 0.20)
        max_hold_days = (5, 10, 15)
        gain_ranges = ((7.0, 15.0), (7.0, 20.0), (10.0, 20.0))
        max_signal_highs = (98.0, 100.0)
        close_ranges = ((90.0, 93.0), (90.0, 95.0), (92.0, 95.0))
        volume_only_values = SWEEP_VOLUME_ONLY
    elif args.core_sweep:
        stop_pcts = SWEEP_STOP_PCTS
        target_pcts = SWEEP_TARGET_PCTS
        max_hold_days = SWEEP_MAX_HOLD_DAYS
        gain_ranges = ((5.0, 15.0), (7.0, 15.0), (7.0, 20.0))
        max_signal_highs = (98.0, 100.0)
        close_ranges = ((90.0, 95.0), (92.0, 95.0))
        volume_only_values = SWEEP_VOLUME_ONLY
    else:
        stop_pcts = SWEEP_STOP_PCTS
        target_pcts = SWEEP_TARGET_PCTS
        max_hold_days = SWEEP_MAX_HOLD_DAYS
        gain_ranges = SWEEP_GAIN_RANGES
        max_signal_highs = SWEEP_MAX_SIGNAL_HIGHS
        close_ranges = SWEEP_CLOSE_RANGES
        volume_only_values = SWEEP_VOLUME_ONLY

    params_list: list[dict[str, object]] = []
    for stop_pct, target_pct, hold, gain_range, max_high, close_range, volume_only in product(
        stop_pcts,
        target_pcts,
        max_hold_days,
        gain_ranges,
        max_signal_highs,
        close_ranges,
        volume_only_values,
    ):
        params_list.append(
            {
                "stop_pct": stop_pct,
                "target_pct": target_pct,
                "max_hold_days": hold,
                "gain_range": gain_range,
                "max_signal_high": max_high,
                "close_range": close_range,
                "volume_only": volume_only,
                "preferred_close_min": 92.5,
            }
        )
    return params_list


def make_sweep_param(
    stop_pct: float,
    target_pct: float,
    hold: int,
    gain_range: tuple[float, float],
    max_high: float,
    close_range: tuple[float, float],
    volume_only: bool,
) -> dict[str, object]:
    return {
        "stop_pct": stop_pct,
        "target_pct": target_pct,
        "max_hold_days": hold,
        "gain_range": gain_range,
        "max_signal_high": max_high,
        "close_range": close_range,
        "volume_only": volume_only,
        "preferred_close_min": 92.5,
    }


def params_from_ranked_rows(path: Path, limit: int) -> list[dict[str, object]]:
    ranked = pd.read_csv(path, encoding="utf-8-sig")
    params_list: list[dict[str, object]] = []
    for _, row in ranked.head(limit).iterrows():
        params_list.append(
            make_sweep_param(
                float(row["止损%"]) / 100.0,
                float(row["止盈%"]) / 100.0,
                int(row["最长持有"]),
                (float(row["D0涨幅下限"]), float(row["D0涨幅上限"])),
                float(row["D0最高价上限"]),
                (float(row["收盘价下限"]), float(row["收盘价上限"])),
                bool(row["只选放量"]),
            )
        )
    return params_list


def fixed_signal_exit_grid() -> list[dict[str, object]]:
    params_list: list[dict[str, object]] = []
    for stop_pct, target_pct, hold in product(SWEEP_STOP_PCTS, SWEEP_TARGET_PCTS, SWEEP_MAX_HOLD_DAYS):
        params_list.append(
            make_sweep_param(
                stop_pct,
                target_pct,
                hold,
                (7.0, 15.0),
                98.0,
                (90.0, 95.0),
                False,
            )
        )
    return params_list


def staged_signal_grid(exit_params: list[dict[str, object]]) -> list[dict[str, object]]:
    params_list: list[dict[str, object]] = []
    for exit_param, gain_range, max_high, close_range, volume_only in product(
        exit_params,
        ((5.0, 15.0), (7.0, 15.0), (7.0, 20.0), (10.0, 20.0)),
        (96.0, 98.0, 100.0),
        ((90.0, 95.0), (92.0, 95.0), (90.0, 93.0)),
        SWEEP_VOLUME_ONLY,
    ):
        params_list.append(
            make_sweep_param(
                float(exit_param["stop_pct"]),
                float(exit_param["target_pct"]),
                int(exit_param["max_hold_days"]),
                gain_range,
                max_high,
                close_range,
                volume_only,
            )
        )
    return params_list


def evaluate_params(
    records: list[dict[str, object]],
    params_list: list[dict[str, object]],
    args: argparse.Namespace,
) -> tuple[pd.DataFrame, dict[str, object] | None]:
    rows: list[dict[str, object]] = []
    best_params: dict[str, object] | None = None
    best_key = None

    for idx, params in enumerate(params_list, start=1):
        if args.sweep_progress and (idx == 1 or idx % 100 == 0 or idx == len(params_list)):
            print(f"扫描进度: {idx}/{len(params_list)}")
        train_trades = simulate_sweep_records(records, params, args.train_start, args.train_end)
        valid_trades = simulate_sweep_records(records, params, args.valid_start, args.valid_end)
        if len(valid_trades) < args.min_valid_trades:
            continue
        test_trades = simulate_sweep_records(records, params, args.test_start, args.test_end)

        train_metrics = metrics_for_trades(train_trades)
        valid_metrics = metrics_for_trades(valid_trades)
        test_metrics = metrics_for_trades(test_trades)
        row = params_label(params)
        row.update(
            {
                "训练期交易数": int(train_metrics["trades"]),
                "训练期胜率%": train_metrics["win_rate"],
                "训练期累计收益%": train_metrics["cum_return"],
                "验证期交易数": int(valid_metrics["trades"]),
                "验证期胜率%": valid_metrics["win_rate"],
                "验证期平均收益%": valid_metrics["avg_return"],
                "验证期中位收益%": valid_metrics["median_return"],
                "验证期累计收益%": valid_metrics["cum_return"],
                "验证期最大单笔亏损%": valid_metrics["max_loss"],
                "验证期最大单笔收益%": valid_metrics["max_gain"],
                "测试期交易数": int(test_metrics["trades"]),
                "测试期胜率%": test_metrics["win_rate"],
                "测试期平均收益%": test_metrics["avg_return"],
                "测试期中位收益%": test_metrics["median_return"],
                "测试期累计收益%": test_metrics["cum_return"],
                "测试期最大单笔亏损%": test_metrics["max_loss"],
                "测试期最大单笔收益%": test_metrics["max_gain"],
            }
        )

        if args.rolling_start_eval:
            rolling_valid = rolling_start_results(records, params, args.valid_start, args.valid_end)
            rolling_valid_metrics = rolling_metrics(rolling_valid)
            if rolling_valid_metrics["starts"] < args.min_rolling_starts:
                continue
            row.update(
                {
                    "滚动起点数": int(rolling_valid_metrics["starts"]),
                    "滚动平均累计收益%": rolling_valid_metrics["mean"],
                    "滚动中位累计收益%": rolling_valid_metrics["median"],
                    "滚动最差累计收益%": rolling_valid_metrics["worst"],
                    "滚动25分位收益%": rolling_valid_metrics["p25"],
                    "滚动正收益占比%": rolling_valid_metrics["positive_rate"],
                    "滚动收益标准差": rolling_valid_metrics["std"],
                }
            )

        if args.monte_carlo_eval:
            mc_valid = monte_carlo_paths(
                records,
                params,
                args.valid_start,
                args.valid_end,
                args.mc_runs,
                args.mc_wait_min,
                args.mc_wait_max,
                args.mc_seed + idx * 100000,
                args.mc_wait_unit,
            )
            mc_valid_metrics = monte_carlo_metrics(mc_valid)
            row.update(
                {
                    "MC路径数": int(mc_valid_metrics["runs"]),
                    "MC平均累计收益%": mc_valid_metrics["mean"],
                    "MC中位累计收益%": mc_valid_metrics["median"],
                    "MC 5分位收益%": mc_valid_metrics["p05"],
                    "MC最差累计收益%": mc_valid_metrics["worst"],
                    "MC正收益占比%": mc_valid_metrics["positive_rate"],
                    "MC平均交易数": mc_valid_metrics["avg_trades"],
                    "MC收益标准差": mc_valid_metrics["std"],
                }
            )

        rows.append(row)
        if args.monte_carlo_eval:
            candidate_key = (
                row["MC中位累计收益%"],
                row["MC 5分位收益%"],
                row["MC正收益占比%"],
                row["验证期累计收益%"],
            )
        elif args.rolling_start_eval:
            candidate_key = (
                row["滚动中位累计收益%"],
                row["滚动最差累计收益%"],
                row["滚动正收益占比%"],
                row["验证期累计收益%"],
            )
        else:
            candidate_key = (
                row["验证期累计收益%"],
                row["验证期交易数"],
                row["训练期累计收益%"],
            )
        if best_key is None or candidate_key > best_key:
            best_key = candidate_key
            best_params = params

    ranked = pd.DataFrame(rows)
    if not ranked.empty:
        if args.monte_carlo_eval:
            ranked = ranked.sort_values(
                ["MC中位累计收益%", "MC 5分位收益%", "MC正收益占比%", "验证期累计收益%"],
                ascending=[False, False, False, False],
            ).reset_index(drop=True)
        elif args.rolling_start_eval:
            ranked = ranked.sort_values(
                ["滚动中位累计收益%", "滚动最差累计收益%", "滚动正收益占比%", "验证期累计收益%"],
                ascending=[False, False, False, False],
            ).reset_index(drop=True)
        else:
            ranked = ranked.sort_values(
                ["验证期累计收益%", "验证期交易数", "训练期累计收益%"],
                ascending=[False, False, False],
            ).reset_index(drop=True)
    return ranked, best_params


def params_label(params: dict[str, object]) -> dict[str, object]:
    gain_min, gain_max = params["gain_range"]
    close_min, close_max = params["close_range"]
    return {
        "止损%": float(params["stop_pct"]) * 100.0,
        "止盈%": float(params["target_pct"]) * 100.0,
        "最长持有": int(params["max_hold_days"]),
        "D0涨幅下限": gain_min,
        "D0涨幅上限": gain_max,
        "D0最高价上限": float(params["max_signal_high"]),
        "收盘价下限": close_min,
        "收盘价上限": close_max,
        "只选放量": bool(params["volume_only"]),
    }


def run_sweep(args: argparse.Namespace) -> None:
    args.out_dir.mkdir(parents=True, exist_ok=True)
    signals = load_signals(args.signals_file)
    data_end = signals["日期"].max()
    if args.strict_split:
        args.valid_end = pd.Timestamp("2025-12-31")
        args.test_start = pd.Timestamp("2026-01-01")
    elif args.valid_end is None:
        args.valid_end = data_end
    if args.test_end is None:
        args.test_end = data_end

    records = build_sweep_records(signals, args)
    if args.refine_from_ranked:
        params_list = params_from_ranked_rows(args.refine_from_ranked, args.refine_top_n)
        print(f"从已有排名复评参数组合数: {len(params_list)}")
    elif args.staged_sweep:
        stage1_params = fixed_signal_exit_grid()
        print(f"第一阶段退出参数组合数: {len(stage1_params)}")
        stage1_ranked, _ = evaluate_params(records, stage1_params, args)
        top_exit_params = []
        for _, row in stage1_ranked.head(args.staged_top_exits).iterrows():
            top_exit_params.append(
                make_sweep_param(
                    float(row["止损%"]) / 100.0,
                    float(row["止盈%"]) / 100.0,
                    int(row["最长持有"]),
                    (7.0, 15.0),
                    98.0,
                    (90.0, 95.0),
                    False,
                )
            )
        params_list = stage1_params + staged_signal_grid(top_exit_params)
        # Deduplicate equivalent parameter dictionaries.
        unique: dict[tuple[object, ...], dict[str, object]] = {}
        for params in params_list:
            label = params_label(params)
            key = tuple(label.items())
            unique[key] = params
        params_list = list(unique.values())
        print(f"第二阶段合计参数组合数: {len(params_list)}")
    else:
        params_list = sweep_parameter_grid(args)

    ranked, best_params = evaluate_params(records, params_list, args)

    suffix = "_strict" if args.strict_split else ""
    if args.rolling_start_eval:
        suffix = "_rolling"
    if args.monte_carlo_eval:
        suffix = "_monte_carlo"
        if args.mc_wait_unit == "calendar":
            suffix = "_calendar_mc"
    ranked_file = args.out_dir / f"sweep_key_level_100_ranked_params{suffix}.csv"
    ranked.to_csv(ranked_file, index=False, encoding="utf-8-sig", float_format="%.4f")

    best_file = args.out_dir / f"sweep_key_level_100_best_trades{suffix}.csv"
    best_valid_suffix = suffix if suffix == "_calendar_mc" else ""
    best_valid_file = args.out_dir / f"sweep_key_level_100_best_valid_trades{best_valid_suffix}.csv"
    best_test_file = args.out_dir / f"sweep_key_level_100_best_test_2026_trades{best_valid_suffix}.csv"
    best_rolling_valid_file = args.out_dir / "sweep_key_level_100_best_rolling_starts_2025.csv"
    best_rolling_test_file = args.out_dir / "sweep_key_level_100_best_rolling_test_2026.csv"
    mc_file_suffix = "_calendar" if args.mc_wait_unit == "calendar" else ""
    best_mc_valid_file = args.out_dir / f"sweep_key_level_100_best_mc_paths_2025{mc_file_suffix}.csv"
    best_mc_test_file = args.out_dir / f"sweep_key_level_100_best_mc_paths_2026{mc_file_suffix}.csv"
    compare_file = args.out_dir / f"sweep_key_level_100_compare{suffix}.csv"
    if best_params is not None:
        best_trades = simulate_sweep_records(records, best_params, args.valid_start, args.valid_end)
        best_trades.to_csv(best_file, index=False, encoding="utf-8-sig", float_format="%.4f")
        best_trades.to_csv(best_valid_file, index=False, encoding="utf-8-sig", float_format="%.4f")
        best_test_trades = simulate_sweep_records(records, best_params, args.test_start, args.test_end)
        best_test_trades.to_csv(best_test_file, index=False, encoding="utf-8-sig", float_format="%.4f")
        if args.rolling_start_eval:
            rolling_start_results(records, best_params, args.valid_start, args.valid_end).to_csv(
                best_rolling_valid_file,
                index=False,
                encoding="utf-8-sig",
                float_format="%.4f",
            )
            rolling_start_results(records, best_params, args.test_start, args.test_end).to_csv(
                best_rolling_test_file,
                index=False,
                encoding="utf-8-sig",
                float_format="%.4f",
            )
        if args.monte_carlo_eval:
            monte_carlo_paths(
                records,
                best_params,
                args.valid_start,
                args.valid_end,
                args.mc_runs,
                args.mc_wait_min,
                args.mc_wait_max,
                args.mc_seed,
                args.mc_wait_unit,
            ).to_csv(best_mc_valid_file, index=False, encoding="utf-8-sig", float_format="%.4f")
            monte_carlo_paths(
                records,
                best_params,
                args.test_start,
                args.test_end,
                args.mc_runs,
                args.mc_wait_min,
                args.mc_wait_max,
                args.mc_seed + 999999,
                args.mc_wait_unit,
            ).to_csv(best_mc_test_file, index=False, encoding="utf-8-sig", float_format="%.4f")
        current_params = {
            "stop_pct": 0.04,
            "target_pct": 0.10,
            "max_hold_days": 10,
            "gain_range": (7.0, 15.0),
            "max_signal_high": 98.0,
            "close_range": (90.0, 95.0),
            "volume_only": False,
            "preferred_close_min": 92.5,
        }
        strict_params = {
            "stop_pct": 0.08,
            "target_pct": 0.15,
            "max_hold_days": 10,
            "gain_range": (7.0, 20.0),
            "max_signal_high": 100.0,
            "close_range": (90.0, 95.0),
            "volume_only": False,
            "preferred_close_min": 92.5,
        }
        compare_rows = []
        for label, params in [
            ("best", best_params),
            ("strict_8_15_10d", strict_params),
            ("current_4_10_10d", current_params),
        ]:
            valid_trades = simulate_sweep_records(records, params, args.valid_start, args.valid_end)
            test_trades = simulate_sweep_records(records, params, args.test_start, args.test_end)
            valid_metrics = metrics_for_trades(valid_trades)
            test_metrics = metrics_for_trades(test_trades)
            rolling_valid_metrics = rolling_metrics(rolling_start_results(records, params, args.valid_start, args.valid_end))
            rolling_test_metrics = rolling_metrics(rolling_start_results(records, params, args.test_start, args.test_end))
            mc_valid_metrics = monte_carlo_metrics(
                monte_carlo_paths(
                    records,
                    params,
                    args.valid_start,
                    args.valid_end,
                    args.mc_runs if args.monte_carlo_eval else min(args.mc_runs, 100),
                    args.mc_wait_min,
                    args.mc_wait_max,
                    args.mc_seed,
                    args.mc_wait_unit,
                )
            )
            mc_test_metrics = monte_carlo_metrics(
                monte_carlo_paths(
                    records,
                    params,
                    args.test_start,
                    args.test_end,
                    args.mc_runs if args.monte_carlo_eval else min(args.mc_runs, 100),
                    args.mc_wait_min,
                    args.mc_wait_max,
                    args.mc_seed + 999999,
                    args.mc_wait_unit,
                )
            )
            row = {"规则": label}
            row.update(params_label(params))
            row.update(
                {
                    "验证期交易数": int(valid_metrics["trades"]),
                    "验证期胜率%": valid_metrics["win_rate"],
                    "验证期累计收益%": valid_metrics["cum_return"],
                    "验证滚动起点数": int(rolling_valid_metrics["starts"]),
                    "验证滚动中位收益%": rolling_valid_metrics["median"],
                    "验证滚动最差收益%": rolling_valid_metrics["worst"],
                    "验证滚动正收益占比%": rolling_valid_metrics["positive_rate"],
                    "测试期交易数": int(test_metrics["trades"]),
                    "测试期胜率%": test_metrics["win_rate"],
                    "测试期累计收益%": test_metrics["cum_return"],
                    "测试滚动起点数": int(rolling_test_metrics["starts"]),
                    "测试滚动中位收益%": rolling_test_metrics["median"],
                    "测试滚动最差收益%": rolling_test_metrics["worst"],
                    "测试滚动正收益占比%": rolling_test_metrics["positive_rate"],
                    "验证MC中位收益%": mc_valid_metrics["median"],
                    "验证MC 5分位收益%": mc_valid_metrics["p05"],
                    "验证MC正收益占比%": mc_valid_metrics["positive_rate"],
                    "测试MC中位收益%": mc_test_metrics["median"],
                    "测试MC 5分位收益%": mc_test_metrics["p05"],
                    "测试MC正收益占比%": mc_test_metrics["positive_rate"],
                }
            )
            compare_rows.append(row)
        pd.DataFrame(compare_rows).to_csv(compare_file, index=False, encoding="utf-8-sig", float_format="%.4f")

    print(f"缓存候选交易数: {len(records)}")
    print(f"扫描参数组合数: {len(params_list)}")
    print(f"满足验证期交易数限制的组合数: {len(ranked)}")
    print(f"参数排名: {ranked_file}")
    if best_params is not None:
        print(f"最佳规则交易明细: {best_file}")
        print(f"最佳规则验证期交易明细: {best_valid_file}")
        print(f"最佳规则测试期交易明细: {best_test_file}")
        if args.rolling_start_eval:
            print(f"最佳规则2025滚动起点: {best_rolling_valid_file}")
            print(f"最佳规则2026滚动起点: {best_rolling_test_file}")
        if args.monte_carlo_eval:
            print(f"最佳规则2025随机路径: {best_mc_valid_file}")
            print(f"最佳规则2026随机路径: {best_mc_test_file}")
        print(f"最佳规则对比: {compare_file}")
        print("最佳参数:")
        print(pd.DataFrame([ranked.iloc[0]]).to_string(index=False))


def skip_row(signal_date: pd.Timestamp, code: str, name: str, reason: str) -> dict[str, str]:
    return {
        "信号日": signal_date.strftime("%Y-%m-%d"),
        "股票代码": normalize_code(code),
        "股票名称": str(name),
        "原因": reason,
    }


def summarize_trades(trades: pd.DataFrame) -> pd.DataFrame:
    if trades.empty:
        return pd.DataFrame(
            columns=["月份", "交易次数", "胜率%", "平均收益率%", "中位收益率%", "最大单笔收益%", "最大单笔亏损%"]
        )

    monthly = trades.copy()
    monthly["月份"] = pd.to_datetime(monthly["买入日"]).dt.to_period("M").astype(str)
    summary = (
        monthly.groupby("月份")
        .agg(
            交易次数=("股票代码", "size"),
            胜率=("收益率%", lambda series: (series > 0).mean() * 100.0),
            平均收益率=("收益率%", "mean"),
            中位收益率=("收益率%", "median"),
            最大单笔收益=("收益率%", "max"),
            最大单笔亏损=("收益率%", "min"),
        )
        .reset_index()
    )
    total = pd.DataFrame(
        [
            {
                "月份": "合计",
                "交易次数": len(trades),
                "胜率": (trades["收益率%"] > 0).mean() * 100.0,
                "平均收益率": trades["收益率%"].mean(),
                "中位收益率": trades["收益率%"].median(),
                "最大单笔收益": trades["收益率%"].max(),
                "最大单笔亏损": trades["收益率%"].min(),
            }
        ]
    )
    summary = pd.concat([summary, total], ignore_index=True)
    return summary.rename(
        columns={
            "胜率": "胜率%",
            "平均收益率": "平均收益率%",
            "中位收益率": "中位收益率%",
            "最大单笔收益": "最大单笔收益%",
            "最大单笔亏损": "最大单笔亏损%",
        }
    )


def cumulative_return(trades: pd.DataFrame) -> float:
    equity = 1.0
    for value in trades["收益率%"]:
        equity *= 1.0 + float(value) / 100.0
    return (equity - 1.0) * 100.0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Backtest key-level approach signals with configurable exits.")
    parser.add_argument("--signals-file", type=Path, default=DEFAULT_SIGNALS_FILE)
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    parser.add_argument("--prefix", default="key_level_100_trailing_stop_2026_q1")
    parser.add_argument("--start-date", type=pd.Timestamp, default=pd.Timestamp("2026-01-01"))
    parser.add_argument("--end-date", type=pd.Timestamp, default=pd.Timestamp("2026-03-31"))
    parser.add_argument("--key-level", type=float, default=100.0)
    parser.add_argument("--trend-state", default="up")
    parser.add_argument("--close-min", type=float, default=90.0)
    parser.add_argument("--close-max", type=float, default=95.0)
    parser.add_argument("--gain-min", type=float, default=7.0)
    parser.add_argument("--gain-max", type=float, default=15.0)
    parser.add_argument("--strong-gain-min", type=float, default=10.0)
    parser.add_argument("--preferred-close-min", type=float, default=92.5)
    parser.add_argument("--max-signal-high", type=float, default=98.0)
    parser.add_argument("--exit-mode", choices=["trailing-stop", "fixed-target-stop"], default="trailing-stop")
    parser.add_argument("--stop-pct", type=float, default=0.04)
    parser.add_argument("--target-pct", type=float, default=0.10)
    parser.add_argument("--trailing-drawdown", type=float, default=0.04)
    parser.add_argument("--max-hold-days", type=int, default=10)
    parser.add_argument("--max-bad-gap", type=float, default=0.35)
    parser.add_argument("--sweep", action="store_true")
    parser.add_argument("--smoke-sweep", action="store_true")
    parser.add_argument("--core-sweep", action="store_true")
    parser.add_argument("--calendar-mc-grid", action="store_true")
    parser.add_argument("--staged-sweep", action="store_true")
    parser.add_argument("--staged-top-exits", type=int, default=10)
    parser.add_argument("--sweep-progress", action="store_true")
    parser.add_argument("--train-start", type=pd.Timestamp, default=pd.Timestamp("2020-01-01"))
    parser.add_argument("--train-end", type=pd.Timestamp, default=pd.Timestamp("2024-12-31"))
    parser.add_argument("--valid-start", type=pd.Timestamp, default=pd.Timestamp("2025-01-01"))
    parser.add_argument("--valid-end", type=pd.Timestamp, default=None)
    parser.add_argument("--test-start", type=pd.Timestamp, default=pd.Timestamp("2026-01-01"))
    parser.add_argument("--test-end", type=pd.Timestamp, default=None)
    parser.add_argument("--strict-split", action="store_true")
    parser.add_argument("--min-valid-trades", type=int, default=30)
    parser.add_argument("--rolling-start-eval", action="store_true")
    parser.add_argument("--min-rolling-starts", type=int, default=30)
    parser.add_argument("--monte-carlo-eval", action="store_true")
    parser.add_argument("--mc-runs", type=int, default=1000)
    parser.add_argument("--mc-wait-min", type=int, default=1)
    parser.add_argument("--mc-wait-max", type=int, default=10)
    parser.add_argument("--mc-wait-unit", choices=["trading", "calendar"], default="trading")
    parser.add_argument("--mc-seed", type=int, default=20260627)
    parser.add_argument("--refine-from-ranked", type=Path, default=None)
    parser.add_argument("--refine-top-n", type=int, default=30)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.sweep:
        run_sweep(args)
        return

    args.out_dir.mkdir(parents=True, exist_ok=True)

    signals = load_signals(args.signals_file)
    base, daily = build_candidates(signals, args)
    trades, skips = simulate_trades(daily, args)
    summary = summarize_trades(trades)

    trades_file = args.out_dir / f"{args.prefix}_trades.csv"
    summary_file = args.out_dir / f"{args.prefix}_summary.csv"
    skips_file = args.out_dir / f"{args.prefix}_skips.csv"

    trades.to_csv(trades_file, index=False, encoding="utf-8-sig", float_format="%.4f")
    summary.to_csv(summary_file, index=False, encoding="utf-8-sig", float_format="%.2f")
    skips.to_csv(skips_file, index=False, encoding="utf-8-sig")

    print(f"基础信号数: {len(base)}")
    print(f"每日最高分信号数: {len(daily)}")
    print(f"实际交易数: {len(trades)}")
    print(f"跳过信号数: {len(skips)}")
    if not trades.empty:
        print(f"复利累计收益率%: {cumulative_return(trades):.2f}")
    print(f"交易明细: {trades_file}")
    print(f"汇总: {summary_file}")
    print(f"跳过记录: {skips_file}")


if __name__ == "__main__":
    main()
