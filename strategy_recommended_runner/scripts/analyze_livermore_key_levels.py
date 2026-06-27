from __future__ import annotations

import argparse
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path
from typing import Iterable

import pandas as pd
from tqdm import tqdm


BASE_DIR = Path("a_stock_data")
PARQUET_DIR = BASE_DIR / "parquet"
META_DIR = BASE_DIR / "meta"
STOCK_LIST_FILE = META_DIR / "stock_list.csv"

DEFAULT_OUT_DIR = Path("strategy_recommended_runner") / "outputs" / "livermore_key_levels"
KEY_LEVELS = (10.0, 20.0, 30.0, 50.0, 100.0, 200.0, 300.0, 500.0, 1000.0)
HORIZONS = (5, 10, 20)
TARGET_PCT = 0.10
STOP_PCT = 0.05
APPROACH_RATIO = 0.90
FIRST_BREAKOUT_LOOKBACK_DAYS = 252
VOLUME_LOOKBACK_DAYS = 20
VOLUME_EXPANSION_RATIO = 1.5
TREND_THRESHOLD = 0.07

REQUIRED_COLUMNS = ["date", "code", "open", "high", "low", "close", "volume", "amount", "turn", "pct_chg"]

CSV_COLUMN_LABELS = {
    "code": "股票代码",
    "name": "股票名称",
    "date": "日期",
    "event_type": "事件类型",
    "event_subtype": "事件子类型",
    "key_level": "关键价位",
    "approach_lower": "接近区间下沿",
    "entry_price": "入场基准价",
    "open": "开盘价",
    "high": "最高价",
    "low": "最低价",
    "close": "收盘价",
    "prev_close": "前收盘价",
    "close_confirmed": "收盘确认突破",
    "is_first_breakout": "是否首次突破",
    "year": "年份",
    "price_bucket": "价格区间",
    "volume_expanded": "成交量放大",
    "market_env": "市场环境",
    "lookback_days": "回看天数",
    "return_price_basis": "收益价格口径",
    "return_5d": "5日收益率",
    "win_5d": "5日是否盈利",
    "insufficient_5d": "5日数据不足",
    "return_10d": "10日收益率",
    "win_10d": "10日是否盈利",
    "insufficient_10d": "10日数据不足",
    "return_20d": "20日收益率",
    "win_20d": "20日是否盈利",
    "insufficient_20d": "20日数据不足",
    "mfe_20d": "20日最大有利波动",
    "mae_20d": "20日最大不利波动",
    "target_before_stop": "先止盈后止损",
    "insufficient_forward_days": "未来数据不足",
    "no_close_back_below_key_20d": "20日未收回关键价下方",
    "break_success": "突破成功",
    "break_date": "突破日期",
    "invalidated_before_break": "突破前先失效",
    "event_success": "事件成功",
    "approach_event": "接近事件",
    "breakout_close": "收盘突破",
    "breakout_intraday": "盘中突破",
    "first_breakout_event": "首次突破事件",
    "sample_count": "样本数",
    "break_success_rate": "突破成功率",
    "win_rate_5d": "5日胜率",
    "win_rate_10d": "10日胜率",
    "win_rate_20d": "20日胜率",
    "avg_return_5d": "5日平均收益率",
    "avg_return_10d": "10日平均收益率",
    "avg_return_20d": "20日平均收益率",
    "median_return_5d": "5日收益率中位数",
    "median_return_10d": "10日收益率中位数",
    "median_return_20d": "20日收益率中位数",
    "max_favorable_excursion": "最大有利波动",
    "max_adverse_excursion": "最大不利波动",
    "target_before_stop_rate": "先止盈后止损比例",
    "expectancy": "期望收益",
    "target_stop_expectancy": "止盈止损期望",
    "trend_state": "趋势状态",
    "trend_threshold": "趋势阈值",
    "trend_confirm_date": "趋势确认日期",
    "trend_ref_low": "趋势参考低点",
    "trend_ref_high": "趋势参考高点",
}


def normalize_code(code) -> str:
    code = str(code).strip()
    if "." in code:
        code = code.split(".")[-1]
    return code.zfill(6)


def load_stock_name_map() -> dict[str, str]:
    if not STOCK_LIST_FILE.exists():
        return {}

    stock_list = pd.read_csv(STOCK_LIST_FILE, dtype=str)
    if "code" not in stock_list.columns:
        return {}

    name_col = "name" if "name" in stock_list.columns else "code_name" if "code_name" in stock_list.columns else None
    if name_col is None:
        return {}

    stock_list = stock_list.copy()
    stock_list["code"] = stock_list["code"].apply(normalize_code)
    stock_list[name_col] = stock_list[name_col].astype(str)
    return dict(zip(stock_list["code"], stock_list[name_col]))


def is_st_stock(name: str) -> bool:
    return "ST" in str(name).upper()


def price_bucket(price: float) -> str:
    buckets = [
        (0.0, 10.0, "<10"),
        (10.0, 20.0, "10-20"),
        (20.0, 30.0, "20-30"),
        (30.0, 50.0, "30-50"),
        (50.0, 100.0, "50-100"),
        (100.0, 200.0, "100-200"),
        (200.0, 300.0, "200-300"),
        (300.0, 500.0, "300-500"),
        (500.0, 1000.0, "500-1000"),
    ]
    for low, high, label in buckets:
        if low <= price < high:
            return label
    return ">=1000"


def load_price_frame(path: Path) -> pd.DataFrame:
    df = pd.read_parquet(path)
    if df.empty:
        return df

    df = df.copy()
    columns = [c for c in REQUIRED_COLUMNS if c in df.columns]
    df = df.loc[:, columns]
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df["code"] = df["code"].apply(normalize_code)
    for column in ["open", "high", "low", "close", "volume", "amount", "turn", "pct_chg"]:
        if column in df.columns:
            df[column] = pd.to_numeric(df[column], errors="coerce")

    required = ["date", "code", "open", "high", "low", "close"]
    df = df.dropna(subset=required)
    return df.sort_values("date").reset_index(drop=True)


def add_trend_state(df: pd.DataFrame, threshold: float = TREND_THRESHOLD) -> pd.DataFrame:
    if df.empty:
        return df

    out = df.copy()
    states: list[str] = []
    confirm_dates: list[object] = []
    ref_lows: list[object] = []
    ref_highs: list[object] = []

    trend: str | None = None
    confirm_date: pd.Timestamp | None = None
    low_i = high_i = 0
    low_p = float(out.loc[0, "low"])
    high_p = float(out.loc[0, "high"])
    trend_ref_low: float | None = None
    trend_ref_high: float | None = None

    for i, row in out.iterrows():
        high = float(row["high"])
        low = float(row["low"])
        date = pd.Timestamp(row["date"])

        if trend is None:
            if low < low_p:
                low_p = low
                low_i = int(i)
            if high > high_p:
                high_p = high
                high_i = int(i)

            if high >= low_p * (1.0 + threshold):
                trend = "up"
                confirm_date = date
                trend_ref_low = low_p
                trend_ref_high = high
                high_p = high
                high_i = int(i)
            elif low <= high_p * (1.0 - threshold):
                trend = "down"
                confirm_date = date
                trend_ref_high = high_p
                trend_ref_low = low
                low_p = low
                low_i = int(i)

        elif trend == "up":
            if high > high_p:
                high_p = high
                high_i = int(i)
                trend_ref_high = high_p
            elif low <= high_p * (1.0 - threshold):
                trend = "down"
                confirm_date = date
                trend_ref_high = high_p
                trend_ref_low = low
                low_p = low
                low_i = int(i)

        else:
            if low < low_p:
                low_p = low
                low_i = int(i)
                trend_ref_low = low_p
            elif high >= low_p * (1.0 + threshold):
                trend = "up"
                confirm_date = date
                trend_ref_low = low_p
                trend_ref_high = high
                high_p = high
                high_i = int(i)

        states.append(trend or "unknown")
        confirm_dates.append(confirm_date.strftime("%Y-%m-%d") if confirm_date is not None else pd.NA)
        ref_lows.append(trend_ref_low if trend_ref_low is not None else pd.NA)
        ref_highs.append(trend_ref_high if trend_ref_high is not None else pd.NA)

    out["trend_state"] = states
    out["trend_threshold"] = threshold
    out["trend_confirm_date"] = confirm_dates
    out["trend_ref_low"] = ref_lows
    out["trend_ref_high"] = ref_highs
    return out


def first_hit_target_before_stop(
    future: pd.DataFrame,
    entry_price: float,
    target_pct: float = TARGET_PCT,
    stop_pct: float = STOP_PCT,
) -> bool | None:
    if future.empty:
        return None

    target_price = entry_price * (1.0 + target_pct)
    stop_price = entry_price * (1.0 - stop_pct)

    for _, row in future.iterrows():
        hit_stop = float(row["low"]) <= stop_price
        hit_target = float(row["high"]) >= target_price
        if hit_stop and hit_target:
            return False
        if hit_stop:
            return False
        if hit_target:
            return True
    return False


def approach_break_success(future: pd.DataFrame, key_level: float, lower: float) -> tuple[bool | None, str | None, bool | None]:
    if future.empty:
        return None, None, None

    for _, row in future.iterrows():
        if float(row["low"]) < lower:
            return False, None, True
        if float(row["high"]) >= key_level:
            return True, pd.Timestamp(row["date"]).strftime("%Y-%m-%d"), False
    return False, None, False


def forward_metrics(df: pd.DataFrame, index: int, entry_price: float, key_level: float, event_type: str) -> dict[str, object]:
    metrics: dict[str, object] = {}

    for horizon in HORIZONS:
        target_index = index + horizon
        if target_index < len(df):
            close_n = float(df.loc[target_index, "close"])
            metrics[f"return_{horizon}d"] = close_n / entry_price - 1.0
            metrics[f"win_{horizon}d"] = metrics[f"return_{horizon}d"] > 0.0
            metrics[f"insufficient_{horizon}d"] = False
        else:
            metrics[f"return_{horizon}d"] = pd.NA
            metrics[f"win_{horizon}d"] = pd.NA
            metrics[f"insufficient_{horizon}d"] = True

    future20 = df.iloc[index + 1:index + 21]
    if len(future20) >= 1:
        metrics["mfe_20d"] = float(future20["high"].max()) / entry_price - 1.0
        metrics["mae_20d"] = float(future20["low"].min()) / entry_price - 1.0
        metrics["target_before_stop"] = first_hit_target_before_stop(future20, entry_price)
        metrics["insufficient_forward_days"] = len(future20) < 20
    else:
        metrics["mfe_20d"] = pd.NA
        metrics["mae_20d"] = pd.NA
        metrics["target_before_stop"] = pd.NA
        metrics["insufficient_forward_days"] = True

    if event_type == "first_breakout_event" and len(future20) >= 1:
        metrics["no_close_back_below_key_20d"] = not (future20["close"] < key_level).any()
    elif event_type == "first_breakout_event":
        metrics["no_close_back_below_key_20d"] = pd.NA
    else:
        metrics["no_close_back_below_key_20d"] = pd.NA

    return metrics


def prior_volume_expanded(df: pd.DataFrame, index: int) -> bool | None:
    if "volume" not in df.columns or index < VOLUME_LOOKBACK_DAYS:
        return None
    prior = df.iloc[index - VOLUME_LOOKBACK_DAYS:index]["volume"].dropna()
    if len(prior) < VOLUME_LOOKBACK_DAYS:
        return None
    mean_volume = float(prior.mean())
    if mean_volume <= 0:
        return None
    return float(df.loc[index, "volume"]) > mean_volume * VOLUME_EXPANSION_RATIO


def event_base_row(
    df: pd.DataFrame,
    index: int,
    code: str,
    name: str,
    event_type: str,
    event_subtype: str,
    key_level: float,
    close_confirmed: bool,
    is_first_breakout: bool,
) -> dict[str, object]:
    close = float(df.loc[index, "close"])
    lower = key_level * APPROACH_RATIO
    return {
        "code": code,
        "name": name,
        "date": pd.Timestamp(df.loc[index, "date"]).strftime("%Y-%m-%d"),
        "event_type": event_type,
        "event_subtype": event_subtype,
        "key_level": key_level,
        "approach_lower": lower,
        "entry_price": close,
        "open": float(df.loc[index, "open"]),
        "high": float(df.loc[index, "high"]),
        "low": float(df.loc[index, "low"]),
        "close": close,
        "prev_close": float(df.loc[index - 1, "close"]) if index > 0 else pd.NA,
        "close_confirmed": close_confirmed,
        "is_first_breakout": is_first_breakout,
        "year": int(pd.Timestamp(df.loc[index, "date"]).year),
        "price_bucket": price_bucket(close),
        "volume_expanded": prior_volume_expanded(df, index),
        "market_env": "unknown",
        "lookback_days": FIRST_BREAKOUT_LOOKBACK_DAYS if event_type == "first_breakout_event" else pd.NA,
        "return_price_basis": "raw_unadjusted",
        "trend_state": df.loc[index, "trend_state"],
        "trend_threshold": float(df.loc[index, "trend_threshold"]),
        "trend_confirm_date": df.loc[index, "trend_confirm_date"],
        "trend_ref_low": df.loc[index, "trend_ref_low"],
        "trend_ref_high": df.loc[index, "trend_ref_high"],
    }


def build_stock_events(df: pd.DataFrame, code: str, name: str) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    events: list[dict[str, object]] = []
    signals: list[dict[str, object]] = []

    if len(df) < 2:
        return events, signals

    if "trend_state" not in df.columns:
        df = add_trend_state(df)

    for key_level in KEY_LEVELS:
        lower = key_level * APPROACH_RATIO
        uptrend = df["trend_state"] == "up"
        in_approach = (df["close"] >= lower) & (df["close"] < key_level)
        approach_active = in_approach & uptrend
        approach_entry = approach_active & ~approach_active.shift(1, fill_value=False)

        close_breakout = (df["close"].shift(1) < key_level) & (df["close"] >= key_level) & uptrend
        intraday_breakout = (df["close"].shift(1) < key_level) & (df["high"] >= key_level) & uptrend
        prior_close_max = df["close"].shift(1).rolling(FIRST_BREAKOUT_LOOKBACK_DAYS, min_periods=1).max()
        first_breakout = close_breakout & (prior_close_max < key_level) & uptrend

        signal_mask = approach_entry | close_breakout | intraday_breakout | first_breakout
        for index in signal_mask[signal_mask].index:
            signal_row = {
                "code": code,
                "name": name,
                "date": pd.Timestamp(df.loc[index, "date"]).strftime("%Y-%m-%d"),
                "key_level": key_level,
                "approach_lower": lower,
                "close": float(df.loc[index, "close"]),
                "high": float(df.loc[index, "high"]),
                "prev_close": float(df.loc[index - 1, "close"]) if index > 0 else pd.NA,
                "approach_event": bool(approach_entry.loc[index]),
                "breakout_close": bool(close_breakout.loc[index]),
                "breakout_intraday": bool(intraday_breakout.loc[index]),
                "first_breakout_event": bool(first_breakout.loc[index]),
                "volume_expanded": prior_volume_expanded(df, int(index)),
                "market_env": "unknown",
                "return_price_basis": "raw_unadjusted",
                "trend_state": df.loc[index, "trend_state"],
                "trend_threshold": float(df.loc[index, "trend_threshold"]),
                "trend_confirm_date": df.loc[index, "trend_confirm_date"],
                "trend_ref_low": df.loc[index, "trend_ref_low"],
                "trend_ref_high": df.loc[index, "trend_ref_high"],
            }
            signals.append(signal_row)

        for index in approach_entry[approach_entry].index:
            index = int(index)
            base = event_base_row(
                df, index, code, name, "approach_event", "close_in_zone", key_level, False, False
            )
            future20 = df.iloc[index + 1:index + 21]
            success, break_date, invalidated = approach_break_success(future20, key_level, lower)
            base.update(forward_metrics(df, index, float(base["entry_price"]), key_level, "approach_event"))
            base.update({
                "break_success": success,
                "break_date": break_date,
                "invalidated_before_break": invalidated,
                "event_success": success,
            })
            events.append(base)

        for index in close_breakout[close_breakout].index:
            index = int(index)
            is_first = bool(first_breakout.loc[index])
            base = event_base_row(
                df, index, code, name, "breakout_event", "breakout_close", key_level, True, is_first
            )
            base.update(forward_metrics(df, index, float(base["entry_price"]), key_level, "breakout_event"))
            base.update({
                "break_success": base["target_before_stop"],
                "break_date": pd.NA,
                "invalidated_before_break": pd.NA,
                "event_success": base["target_before_stop"],
            })
            events.append(base)

        pure_intraday = intraday_breakout & ~close_breakout
        for index in pure_intraday[pure_intraday].index:
            index = int(index)
            base = event_base_row(
                df, index, code, name, "breakout_event", "breakout_intraday", key_level, False, False
            )
            base.update(forward_metrics(df, index, float(base["entry_price"]), key_level, "breakout_event"))
            base.update({
                "break_success": base["target_before_stop"],
                "break_date": pd.NA,
                "invalidated_before_break": pd.NA,
                "event_success": base["target_before_stop"],
            })
            events.append(base)

        for index in first_breakout[first_breakout].index:
            index = int(index)
            base = event_base_row(
                df, index, code, name, "first_breakout_event", "first_breakout_close", key_level, True, True
            )
            base.update(forward_metrics(df, index, float(base["entry_price"]), key_level, "first_breakout_event"))
            no_close_back = base["no_close_back_below_key_20d"]
            target_first = base["target_before_stop"]
            success = bool(target_first and no_close_back) if not pd.isna(target_first) and not pd.isna(no_close_back) else pd.NA
            base.update({
                "break_success": success,
                "break_date": pd.NA,
                "invalidated_before_break": pd.NA,
                "event_success": success,
            })
            events.append(base)

    return events, signals


def bool_rate(series: pd.Series) -> float | pd.NA:
    clean = series.dropna()
    if clean.empty:
        return pd.NA
    return clean.astype(bool).mean()


def summarize(events: pd.DataFrame, group_cols: list[str]) -> pd.DataFrame:
    rows: list[dict[str, object]] = []
    if events.empty:
        return pd.DataFrame()

    grouped = events.groupby(group_cols, dropna=False)
    for group_key, group in grouped:
        if not isinstance(group_key, tuple):
            group_key = (group_key,)
        row = {col: val for col, val in zip(group_cols, group_key)}
        row["sample_count"] = len(group)
        row["break_success_rate"] = bool_rate(group["break_success"])

        for horizon in HORIZONS:
            valid = group[group[f"insufficient_{horizon}d"] == False]
            row[f"win_rate_{horizon}d"] = bool_rate(valid[f"win_{horizon}d"]) if not valid.empty else pd.NA
            row[f"avg_return_{horizon}d"] = valid[f"return_{horizon}d"].mean() if not valid.empty else pd.NA
            row[f"median_return_{horizon}d"] = valid[f"return_{horizon}d"].median() if not valid.empty else pd.NA

        valid20 = group[group["insufficient_forward_days"] == False]
        row["max_favorable_excursion"] = valid20["mfe_20d"].mean() if not valid20.empty else pd.NA
        row["max_adverse_excursion"] = valid20["mae_20d"].mean() if not valid20.empty else pd.NA
        row["target_before_stop_rate"] = bool_rate(valid20["target_before_stop"]) if not valid20.empty else pd.NA
        row["expectancy"] = row["avg_return_20d"]
        if row["target_before_stop_rate"] is pd.NA:
            row["target_stop_expectancy"] = pd.NA
        else:
            rate = float(row["target_before_stop_rate"])
            row["target_stop_expectancy"] = rate * TARGET_PCT - (1.0 - rate) * STOP_PCT
        rows.append(row)

    return pd.DataFrame(rows).sort_values(group_cols).reset_index(drop=True)


def to_chinese_columns(df: pd.DataFrame) -> pd.DataFrame:
    return df.rename(columns={column: CSV_COLUMN_LABELS.get(column, column) for column in df.columns})


def write_csv(df: pd.DataFrame, path: Path) -> None:
    to_chinese_columns(df).to_csv(path, index=False, encoding="utf-8-sig")


def write_report(
    out_dir: Path,
    events: pd.DataFrame,
    signals: pd.DataFrame,
    total_files: int,
    loaded_files: int,
    skipped_st: int,
    data_min: pd.Timestamp | None,
    data_max: pd.Timestamp | None,
    include_st: bool,
    workers: int,
) -> None:
    lines = [
        "# Livermore Key Level Analysis Report",
        "",
        "## Data Scope",
        f"- Parquet files found: {total_files}",
        f"- Parquet files loaded: {loaded_files}",
        f"- ST stocks skipped: {skipped_st if not include_st else 0}",
        f"- Signal rows: {len(signals)}",
        f"- Event rows: {len(events)}",
        f"- Date range: {data_min.strftime('%Y-%m-%d') if data_min is not None else 'n/a'} to {data_max.strftime('%Y-%m-%d') if data_max is not None else 'n/a'}",
        f"- Worker processes: {workers}",
        "",
        "## Price Basis",
        "- Signal detection uses raw unadjusted open/high/low/close from local parquet files.",
        "- Local parquet files do not contain an adjustflag column, but the BaoStock updater is configured with ADJUST_FLAG=3, and the pytdx updater stores ordinary daily prices.",
        "- Return statistics in this run use raw unadjusted prices because no adjusted close/high/low columns are available locally.",
        "",
        "## Parameters",
        f"- Key levels: {', '.join(str(int(x)) for x in KEY_LEVELS)}",
        f"- Approach zone: close in [key_level * {APPROACH_RATIO:.2f}, key_level)",
        f"- First breakout lookback: {FIRST_BREAKOUT_LOOKBACK_DAYS} trading days, excluding current day",
        f"- Trend filter: only events whose signal day is in a {TREND_THRESHOLD:.0%} uptrend are included.",
        f"- Trend rule: a rise of at least {TREND_THRESHOLD:.0%} from the running low confirms uptrend; a fall of at least {TREND_THRESHOLD:.0%} from the running high confirms downtrend.",
        f"- Breakout target/stop: +{TARGET_PCT:.0%} / -{STOP_PCT:.0%} within 20 trading days",
        f"- Volume expansion: current volume > prior {VOLUME_LOOKBACK_DAYS}-day mean * {VOLUME_EXPANSION_RATIO}",
        "- Market environment: unknown, because index history is not present in the local parquet dataset.",
        "",
        "## Output Files",
        "- signals.csv: CSV header uses Chinese column names.",
        "- events.csv: CSV header uses Chinese column names.",
        "- summary_by_key_level.csv: CSV header uses Chinese column names.",
        "- summary_by_event_type.csv: CSV header uses Chinese column names.",
        "- summary_by_year.csv: CSV header uses Chinese column names.",
        "- summary_by_price_bucket.csv: CSV header uses Chinese column names.",
        "- summary_by_market_env.csv: CSV header uses Chinese column names.",
        "- summary_by_volume_expanded.csv: CSV header uses Chinese column names.",
        "- summary_by_first_breakout.csv: CSV header uses Chinese column names.",
        "",
        "## Known Data Risks",
        "- Current stock list contains listed stocks only, so the study has survivorship bias.",
        "- Raw-price returns can be distorted around dividends, splits, and other corporate actions.",
        "- Rows with fewer than 20 future trading days are retained in events.csv but excluded from 20-day summary denominators.",
        "- Same-day target and stop hits are counted conservatively as stop first.",
    ]
    (out_dir / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Analyze Livermore-style psychological key price levels.")
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    parser.add_argument("--include-st", action="store_true", help="Include ST stocks. Default excludes names containing ST.")
    parser.add_argument("--progress", action="store_true", help="Show progress bar.")
    parser.add_argument("--limit", type=int, default=0, help="Limit number of parquet files for smoke testing.")
    parser.add_argument("--workers", type=int, default=8, help="Number of worker processes. Default 8.")
    return parser.parse_args()


def iter_paths(paths: Iterable[Path], progress: bool) -> Iterable[Path]:
    return tqdm(list(paths), desc="analyzing") if progress else paths


def process_stock_file(task: tuple[str, str]) -> dict[str, object]:
    path_text, name = task
    path = Path(path_text)
    code = normalize_code(path.stem)
    df = load_price_frame(path)
    if df.empty:
        return {
            "loaded": 0,
            "events": [],
            "signals": [],
            "data_min": None,
            "data_max": None,
            "error": "",
        }

    df = add_trend_state(df)
    events, signals = build_stock_events(df, code, name)
    return {
        "loaded": 1,
        "events": events,
        "signals": signals,
        "data_min": pd.Timestamp(df["date"].min()).strftime("%Y-%m-%d"),
        "data_max": pd.Timestamp(df["date"].max()).strftime("%Y-%m-%d"),
        "error": "",
    }


def main() -> None:
    args = parse_args()
    if not PARQUET_DIR.exists():
        raise FileNotFoundError(f"Parquet directory not found: {PARQUET_DIR}")

    out_dir: Path = args.out_dir
    out_dir.mkdir(parents=True, exist_ok=True)

    name_map = load_stock_name_map()
    parquet_files = sorted(PARQUET_DIR.glob("*.parquet"))
    if args.limit > 0:
        parquet_files = parquet_files[:args.limit]

    tasks: list[tuple[str, str]] = []
    skipped_st = 0
    for path in parquet_files:
        code = normalize_code(path.stem)
        name = name_map.get(code, "")
        if not args.include_st and is_st_stock(name):
            skipped_st += 1
            continue
        tasks.append((str(path), name))

    all_events: list[dict[str, object]] = []
    all_signals: list[dict[str, object]] = []
    loaded_files = 0
    data_mins: list[pd.Timestamp] = []
    data_maxs: list[pd.Timestamp] = []

    workers = max(1, int(args.workers))
    if workers == 1:
        task_iter = tqdm(tasks, desc="analyzing") if args.progress else tasks
        for task in task_iter:
            try:
                result = process_stock_file(task)
            except Exception as exc:
                print(f"skip read error: {task[0]} {type(exc).__name__}: {exc}")
                continue
            loaded_files += int(result["loaded"])
            all_events.extend(result["events"])
            all_signals.extend(result["signals"])
            if result["data_min"] is not None:
                data_mins.append(pd.Timestamp(result["data_min"]))
            if result["data_max"] is not None:
                data_maxs.append(pd.Timestamp(result["data_max"]))
    else:
        with ProcessPoolExecutor(max_workers=workers) as executor:
            futures = [executor.submit(process_stock_file, task) for task in tasks]
            future_iter = as_completed(futures)
            if args.progress:
                future_iter = tqdm(future_iter, total=len(futures), desc="analyzing")
            for future in future_iter:
                try:
                    result = future.result()
                except Exception as exc:
                    print(f"skip worker error: {type(exc).__name__}: {exc}")
                    continue
                loaded_files += int(result["loaded"])
                all_events.extend(result["events"])
                all_signals.extend(result["signals"])
                if result["data_min"] is not None:
                    data_mins.append(pd.Timestamp(result["data_min"]))
                if result["data_max"] is not None:
                    data_maxs.append(pd.Timestamp(result["data_max"]))

    events_df = pd.DataFrame(all_events)
    signals_df = pd.DataFrame(all_signals)

    write_csv(events_df, out_dir / "events.csv")
    write_csv(signals_df, out_dir / "signals.csv")

    summary_specs = {
        "summary_by_key_level.csv": ["key_level"],
        "summary_by_event_type.csv": ["event_type"],
        "summary_by_year.csv": ["year"],
        "summary_by_price_bucket.csv": ["price_bucket"],
        "summary_by_market_env.csv": ["market_env"],
        "summary_by_volume_expanded.csv": ["volume_expanded"],
        "summary_by_first_breakout.csv": ["is_first_breakout"],
    }
    for filename, group_cols in summary_specs.items():
        summary = summarize(events_df, group_cols)
        write_csv(summary, out_dir / filename)

    write_report(
        out_dir=out_dir,
        events=events_df,
        signals=signals_df,
        total_files=len(parquet_files),
        loaded_files=loaded_files,
        skipped_st=skipped_st,
        data_min=min(data_mins) if data_mins else None,
        data_max=max(data_maxs) if data_maxs else None,
        include_st=args.include_st,
        workers=workers,
    )

    print(f"done: {out_dir}")
    print(f"events: {len(events_df)}, signals: {len(signals_df)}")


if __name__ == "__main__":
    main()
