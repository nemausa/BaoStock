from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd

from backtest_key_level_trailing_stop import normalize_code


DEFAULT_EVENTS_FILE = (
    Path("strategy_recommended_runner") / "outputs" / "livermore_key_levels" / "events.csv"
)
DEFAULT_OUT_DIR = Path("strategy_recommended_runner") / "outputs" / "livermore_key_levels"
PARQUET_DIR = Path("a_stock_data") / "parquet"


def load_events(path: Path) -> pd.DataFrame:
    events = pd.read_csv(path, dtype={"股票代码": str}, encoding="utf-8-sig", low_memory=False)
    events = events.copy()
    events["股票代码"] = events["股票代码"].apply(normalize_code)
    events["日期"] = pd.to_datetime(events["日期"], errors="coerce")
    for column in ["关键价位", "开盘价", "最高价", "最低价", "收盘价", "前收盘价"]:
        events[column] = pd.to_numeric(events[column], errors="coerce")
    events["D0涨幅%"] = (events["收盘价"] / events["前收盘价"] - 1.0) * 100.0
    events["成交量放大_bool"] = events["成交量放大"].astype(str).str.lower().eq("true")
    return events


def load_price_frame(code: str) -> pd.DataFrame | None:
    path = PARQUET_DIR / f"{normalize_code(code)}.parquet"
    if not path.exists():
        return None

    df = pd.read_parquet(path).copy()
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    for column in ["open", "high", "low", "close", "volume"]:
        if column in df.columns:
            df[column] = pd.to_numeric(df[column], errors="coerce")

    required = ["date", "open", "high", "low", "close"]
    return df.dropna(subset=required).sort_values("date").reset_index(drop=True)


def select_breakouts(events: pd.DataFrame, args: argparse.Namespace) -> pd.DataFrame:
    start_date = pd.Timestamp(args.start_date)
    end_date = pd.Timestamp(args.end_date)
    selected = events[
        events["日期"].between(start_date, end_date)
        & events["事件类型"].eq("first_breakout_event")
        & events["关键价位"].eq(args.key_level)
        & events["趋势状态"].eq(args.trend_state)
        & events["收盘确认突破"].eq(True)
        & events["是否首次突破"].eq(True)
    ].copy()
    return selected.sort_values(["日期", "股票代码"]).reset_index(drop=True)


def build_exports(candidates: pd.DataFrame, next_days: int) -> tuple[pd.DataFrame, pd.DataFrame]:
    detail_rows: list[dict[str, object]] = []
    summary_rows: list[dict[str, object]] = []

    for _, signal in candidates.iterrows():
        code = normalize_code(signal["股票代码"])
        df = load_price_frame(code)
        if df is None or df.empty:
            continue

        signal_date = pd.Timestamp(signal["日期"])
        start_indices = df.index[df["date"] >= signal_date]
        if len(start_indices) == 0:
            continue

        start_index = int(start_indices[0])
        window = df.iloc[start_index: start_index + next_days + 1].copy()
        future = window[window["date"] > signal_date].head(next_days).copy()

        d0_open = float(signal["开盘价"])
        d0_high = float(signal["最高价"])
        d0_low = float(signal["最低价"])
        d0_close = float(signal["收盘价"])
        buy_open = float(future.iloc[0]["open"]) if not future.empty else pd.NA
        next_open_above_key = bool(not future.empty and float(future.iloc[0]["open"]) >= 100.0)

        first_below_100 = ""
        if not future.empty:
            below = future[future["low"] < 100.0]
            if not below.empty:
                first_below_100 = pd.Timestamp(below.iloc[0]["date"]).strftime("%Y-%m-%d")

        for day_number, (price_index, row) in enumerate(window.iterrows(), start=0):
            trade_date = pd.Timestamp(row["date"])
            open_price = float(row["open"])
            high_price = float(row["high"])
            low_price = float(row["low"])
            close_price = float(row["close"])
            volume = float(row["volume"]) if "volume" in row and pd.notna(row["volume"]) else pd.NA
            previous_close = (
                float(df.loc[int(price_index) - 1, "close"])
                if int(price_index) > 0 and pd.notna(df.loc[int(price_index) - 1, "close"])
                else pd.NA
            )
            if pd.isna(previous_close) or float(previous_close) <= 0:
                daily_amplitude = pd.NA
                body_amplitude = pd.NA
                upper_shadow_amplitude = pd.NA
                lower_shadow_amplitude = pd.NA
            else:
                previous_close_float = float(previous_close)
                daily_amplitude = (high_price - low_price) / previous_close_float * 100.0
                body_amplitude = abs(close_price - open_price) / previous_close_float * 100.0
                upper_shadow_amplitude = (
                    high_price - max(open_price, close_price)
                ) / previous_close_float * 100.0
                lower_shadow_amplitude = (
                    min(open_price, close_price) - low_price
                ) / previous_close_float * 100.0
            if pd.isna(buy_open):
                return_vs_buy_open = pd.NA
            else:
                return_vs_buy_open = (close_price / float(buy_open) - 1.0) * 100.0

            detail_rows.append(
                {
                    "信号日": signal_date.strftime("%Y-%m-%d"),
                    "股票代码": code,
                    "股票名称": signal["股票名称"],
                    "D0开盘": d0_open,
                    "D0最高": d0_high,
                    "D0最低": d0_low,
                    "D0收盘": d0_close,
                    "D0涨幅%": float(signal["D0涨幅%"]),
                    "是否放量": "是" if bool(signal["成交量放大_bool"]) else "否",
                    "交易日序号": day_number,
                    "交易日期": trade_date.strftime("%Y-%m-%d"),
                    "开盘价": open_price,
                    "最高价": high_price,
                    "最低价": low_price,
                    "收盘价": close_price,
                    "前收盘价": previous_close,
                    "成交量": volume,
                    "日K振幅%": daily_amplitude,
                    "实体幅度%": body_amplitude,
                    "上影线幅度%": upper_shadow_amplitude,
                    "下影线幅度%": lower_shadow_amplitude,
                    "相对D0收盘收益%": (close_price / d0_close - 1.0) * 100.0,
                    "相对次日开盘收益%": return_vs_buy_open,
                    "是否跌破100": "是" if low_price < 100.0 else "否",
                }
            )

        if future.empty:
            ret20 = pd.NA
            max_up = pd.NA
            max_drawdown = pd.NA
        else:
            buy_open_float = float(future.iloc[0]["open"])
            ret20 = (float(future.iloc[-1]["close"]) / buy_open_float - 1.0) * 100.0
            max_up = (float(future["high"].max()) / buy_open_float - 1.0) * 100.0
            max_drawdown = (float(future["low"].min()) / buy_open_float - 1.0) * 100.0

        summary_rows.append(
            {
                "信号日": signal_date.strftime("%Y-%m-%d"),
                "股票代码": code,
                "股票名称": signal["股票名称"],
                "D0开盘": d0_open,
                "D0最高": d0_high,
                "D0最低": d0_low,
                "D0收盘": d0_close,
                "D0涨幅%": float(signal["D0涨幅%"]),
                "是否放量": "是" if bool(signal["成交量放大_bool"]) else "否",
                "次日开盘": buy_open,
                "次日开盘是否>=100": "是" if next_open_above_key else "否",
                "20日收益%": ret20,
                "20日最大上冲%": max_up,
                "20日最大回撤%": max_drawdown,
                "首次跌破100日期": first_below_100,
                "明细交易日数": int(len(window)),
            }
        )

    return pd.DataFrame(detail_rows), pd.DataFrame(summary_rows)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="导出利弗莫尔100关键点首次突破后N个交易日价格走势。")
    parser.add_argument("--events-file", type=Path, default=DEFAULT_EVENTS_FILE)
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    parser.add_argument("--start-date", default="2026-01-01")
    parser.add_argument("--end-date", default="2026-01-31")
    parser.add_argument("--prefix", default="key_level_100_breakout_2026_01_next20")
    parser.add_argument("--key-level", type=float, default=100.0)
    parser.add_argument("--trend-state", default="up")
    parser.add_argument("--next-days", type=int, default=20)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    events = load_events(args.events_file)
    candidates = select_breakouts(events, args)
    detail, summary = build_exports(candidates, args.next_days)

    args.out_dir.mkdir(parents=True, exist_ok=True)
    detail_file = args.out_dir / f"{args.prefix}_prices.csv"
    summary_file = args.out_dir / f"{args.prefix}_summary.csv"
    detail.to_csv(detail_file, index=False, encoding="utf-8-sig", float_format="%.4f")
    summary.to_csv(summary_file, index=False, encoding="utf-8-sig", float_format="%.4f")

    print("筛选: first_breakout_event; key=100; trend=up; close breakout; first breakout")
    print(f"日期: {args.start_date} 至 {args.end_date}")
    print(f"候选数: {len(candidates)}")
    print(f"明细行数: {len(detail)}")
    print(f"摘要行数: {len(summary)}")
    print(f"明细CSV: {detail_file.resolve()}")
    print(f"摘要CSV: {summary_file.resolve()}")


if __name__ == "__main__":
    main()
