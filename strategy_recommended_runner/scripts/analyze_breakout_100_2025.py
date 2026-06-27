from __future__ import annotations

import argparse
from pathlib import Path
from typing import Iterable

import pandas as pd


BASE_DIR = Path("a_stock_data")
PARQUET_DIR = BASE_DIR / "parquet"
DEFAULT_OUT_DIR = Path("strategy_recommended_runner") / "outputs"
DEFAULT_TRADES_FILE = DEFAULT_OUT_DIR / "breakout_100_2025_trades.csv"
DEFAULT_SUMMARY_FILE = DEFAULT_OUT_DIR / "breakout_100_2025_summary.csv"


def normalize_code(code) -> str:
    code = str(code).strip()
    if "." in code:
        code = code.split(".")[-1]
    return code.zfill(6)


def load_price_frame(path: Path) -> pd.DataFrame:
    columns = ["date", "code", "open", "high", "low", "close", "volume"]
    df = pd.read_parquet(path, columns=[col for col in columns if col in pd.read_parquet(path).columns])
    if df.empty:
        return df

    df = df.copy()
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    if "code" not in df.columns:
        df["code"] = path.stem
    df["code"] = df["code"].apply(normalize_code)
    for column in ["open", "high", "low", "close", "volume"]:
        df[column] = pd.to_numeric(df[column], errors="coerce")

    df = df.dropna(subset=["date", "open", "high", "low", "close", "volume"])
    df = df[
        (df["open"] > 0)
        & (df["high"] > 0)
        & (df["low"] > 0)
        & (df["close"] > 0)
        & (df["volume"] > 0)
    ].copy()
    return df.sort_values("date").reset_index(drop=True)


def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    out["prev_close"] = out["close"].shift(1)
    out["ma20"] = out["close"].rolling(20, min_periods=20).mean()
    out["ma50"] = out["close"].rolling(50, min_periods=50).mean()
    out["volume_mean20_prev"] = out["volume"].shift(1).rolling(20, min_periods=20).mean()
    out["volume_ratio"] = out["volume"] / out["volume_mean20_prev"]
    return out


def signal_mask(df: pd.DataFrame, args: argparse.Namespace) -> pd.Series:
    return (
        (df["date"] >= args.start_date)
        & (df["date"] <= args.end_date)
        & (df["prev_close"] <= args.key_price)
        & (df["close"] > args.key_price)
        & (df["close"] <= args.max_buy_close)
        & (df["volume"] > df["volume_mean20_prev"] * args.volume_multiple)
        & (df["close"] > df["ma20"])
        & (df["ma20"] > df["ma50"])
    )


def evaluate_trade(df: pd.DataFrame, buy_index: int, args: argparse.Namespace) -> dict[str, object]:
    code = normalize_code(df.loc[buy_index, "code"])
    buy_date = pd.Timestamp(df.loc[buy_index, "date"])
    buy_price = float(df.loc[buy_index, "close"])
    target_price = buy_price * (1.0 + args.target_pct)
    stop_price = args.key_price

    future = df.iloc[buy_index + 1: buy_index + 1 + args.hold_days].copy()
    future = future[future["date"] <= args.end_date].copy()

    result = "timeout"
    exit_date = buy_date
    exit_price = buy_price
    exit_index = buy_index

    if not future.empty:
        for idx, row in future.iterrows():
            hit_stop = float(row["close"]) < stop_price
            hit_target = float(row["high"]) >= target_price
            if hit_stop:
                result = "failure"
                exit_date = pd.Timestamp(row["date"])
                exit_price = float(row["close"])
                exit_index = int(idx)
                break
            if hit_target:
                result = "success"
                exit_date = pd.Timestamp(row["date"])
                exit_price = target_price
                exit_index = int(idx)
                break

        if result == "timeout":
            last = future.iloc[-1]
            exit_date = pd.Timestamp(last["date"])
            exit_price = float(last["close"])
            exit_index = int(future.index[-1])

        window = future.loc[:exit_index]
        max_gain = float(window["high"].max()) / buy_price - 1.0
        max_drawdown = float(window["low"].min()) / buy_price - 1.0
        holding_days = int(exit_index - buy_index)
    else:
        max_gain = 0.0
        max_drawdown = 0.0
        holding_days = 0

    return {
        "股票代码": code,
        "买入日期": buy_date.strftime("%Y-%m-%d"),
        "买入价格": buy_price,
        "目标价格": target_price,
        "止损价格": stop_price,
        "退出日期": exit_date.strftime("%Y-%m-%d"),
        "退出价格": exit_price,
        "结果": result,
        "持有天数": holding_days,
        "最大涨幅": max_gain,
        "最大回撤": max_drawdown,
        "成交量倍率": float(df.loc[buy_index, "volume_ratio"]),
        "MA20": float(df.loc[buy_index, "ma20"]),
        "MA50": float(df.loc[buy_index, "ma50"]),
        "收益率": exit_price / buy_price - 1.0,
        "_exit_index": exit_index,
    }


def analyze_stock(path: Path, args: argparse.Namespace) -> list[dict[str, object]]:
    df = load_price_frame(path)
    if len(df) < 51:
        return []

    df = add_indicators(df)
    mask = signal_mask(df, args)
    rows: list[dict[str, object]] = []
    active_until_index = -1

    for buy_index in mask[mask].index:
        buy_index = int(buy_index)
        if buy_index <= active_until_index:
            continue
        trade = evaluate_trade(df, buy_index, args)
        active_until_index = int(trade["_exit_index"])
        trade.pop("_exit_index", None)
        rows.append(trade)

    return rows


def iter_paths(args: argparse.Namespace) -> Iterable[Path]:
    paths = sorted(PARQUET_DIR.glob("*.parquet"))
    if args.limit > 0:
        return paths[: args.limit]
    return paths


def build_summary(trades: pd.DataFrame) -> pd.DataFrame:
    if trades.empty:
        rows = {
            "总样本数": 0,
            "成功数": 0,
            "失败数": 0,
            "超时数": 0,
            "严格胜率": 0.0,
            "有效胜率": 0.0,
            "平均收益": 0.0,
            "平均最大涨幅": 0.0,
            "平均最大回撤": 0.0,
        }
        return pd.DataFrame([rows])

    total = int(len(trades))
    success = int((trades["结果"] == "success").sum())
    failure = int((trades["结果"] == "failure").sum())
    timeout = int((trades["结果"] == "timeout").sum())
    valid_denominator = success + failure
    rows = {
        "总样本数": total,
        "成功数": success,
        "失败数": failure,
        "超时数": timeout,
        "严格胜率": success / total if total else 0.0,
        "有效胜率": success / valid_denominator if valid_denominator else 0.0,
        "平均收益": float(trades["收益率"].mean()),
        "平均最大涨幅": float(trades["最大涨幅"].mean()),
        "平均最大回撤": float(trades["最大回撤"].mean()),
    }
    return pd.DataFrame([rows])


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Analyze 2025 close breakout above 100 yuan strategy.")
    parser.add_argument("--start-date", type=pd.Timestamp, default=pd.Timestamp("2025-01-01"))
    parser.add_argument("--end-date", type=pd.Timestamp, default=pd.Timestamp("2025-12-31"))
    parser.add_argument("--key-price", type=float, default=100.0)
    parser.add_argument("--max-buy-close", type=float, default=105.0)
    parser.add_argument("--volume-multiple", type=float, default=1.2)
    parser.add_argument("--target-pct", type=float, default=0.10)
    parser.add_argument("--hold-days", type=int, default=20)
    parser.add_argument("--trades-file", type=Path, default=DEFAULT_TRADES_FILE)
    parser.add_argument("--summary-file", type=Path, default=DEFAULT_SUMMARY_FILE)
    parser.add_argument("--limit", type=int, default=0, help="Limit number of parquet files for smoke testing.")
    parser.add_argument("--progress", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if not PARQUET_DIR.exists():
        raise FileNotFoundError(f"Parquet directory not found: {PARQUET_DIR}")

    all_rows: list[dict[str, object]] = []
    paths = list(iter_paths(args))
    for number, path in enumerate(paths, start=1):
        try:
            all_rows.extend(analyze_stock(path, args))
        except Exception as exc:
            print(f"skip read/analyze error: {path} {type(exc).__name__}: {exc}")
        if args.progress and (number == 1 or number % 500 == 0 or number == len(paths)):
            print(f"已处理 {number}/{len(paths)}，交易数 {len(all_rows)}")

    trades = pd.DataFrame(all_rows)
    if not trades.empty:
        trades = trades.sort_values(["买入日期", "股票代码"]).reset_index(drop=True)

    summary = build_summary(trades)
    args.trades_file.parent.mkdir(parents=True, exist_ok=True)
    args.summary_file.parent.mkdir(parents=True, exist_ok=True)
    trades.to_csv(args.trades_file, index=False, encoding="utf-8-sig", float_format="%.6f")
    summary.to_csv(args.summary_file, index=False, encoding="utf-8-sig", float_format="%.6f")

    print(summary.to_string(index=False))
    print(f"明细: {args.trades_file.resolve()}")
    print(f"汇总: {args.summary_file.resolve()}")


if __name__ == "__main__":
    main()
