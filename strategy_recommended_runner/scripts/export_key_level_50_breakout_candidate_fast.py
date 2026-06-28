from __future__ import annotations

import argparse
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import date
from pathlib import Path

import pandas as pd

from analyze_livermore_key_levels import (
    FIRST_BREAKOUT_LOOKBACK_DAYS,
    STOCK_LIST_FILE,
    add_trend_state,
    is_st_stock,
    normalize_code,
    prior_volume_expanded,
)
from export_key_level_50_breakout_candidate import output_columns, print_ranked_preview, score_candidates


PARQUET_DIR = Path("a_stock_data") / "parquet"
DEFAULT_OUT_DIR = Path("strategy_recommended_runner") / "outputs" / "livermore_key_levels"
KEY_LEVEL = 50.0


def load_stock_name_map() -> dict[str, str]:
    if not STOCK_LIST_FILE.exists():
        return {}

    stock_list = pd.read_csv(STOCK_LIST_FILE, dtype=str)
    if "code" not in stock_list.columns:
        return {}

    name_column = "name" if "name" in stock_list.columns else "code_name" if "code_name" in stock_list.columns else None
    if name_column is None:
        return {}

    stock_list = stock_list.copy()
    stock_list["code"] = stock_list["code"].apply(normalize_code)
    return dict(zip(stock_list["code"], stock_list[name_column].astype(str)))


def load_price_frame(path: Path) -> pd.DataFrame:
    columns = ["date", "code", "open", "high", "low", "close", "volume"]
    df = pd.read_parquet(path)
    if df.empty:
        return df

    df = df.copy()
    df = df.loc[:, [column for column in columns if column in df.columns]]
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    if "code" not in df.columns:
        df["code"] = path.stem
    df["code"] = df["code"].apply(normalize_code)
    for column in ["open", "high", "low", "close", "volume"]:
        if column in df.columns:
            df[column] = pd.to_numeric(df[column], errors="coerce")

    return (
        df.dropna(subset=["date", "code", "open", "high", "low", "close"])
        .sort_values("date")
        .reset_index(drop=True)
    )


def candidate_from_stock(path: Path, name: str, signal_date: pd.Timestamp, include_st: bool) -> dict[str, object] | None:
    if not include_st and is_st_stock(name):
        return None

    df = load_price_frame(path)
    if len(df) < 2:
        return None

    date_matches = df.index[df["date"].eq(signal_date)]
    if len(date_matches) == 0:
        return None

    index = int(date_matches[0])
    if index == 0:
        return None

    previous_close = float(df.loc[index - 1, "close"])
    row = df.loc[index]
    close_price = float(row["close"])
    prior_close_max = float(
        df.loc[max(0, index - FIRST_BREAKOUT_LOOKBACK_DAYS): index - 1, "close"].max()
    )

    if not (previous_close < KEY_LEVEL and close_price >= KEY_LEVEL and prior_close_max < KEY_LEVEL):
        return None

    df = add_trend_state(df)
    row = df.loc[index]
    if str(row["trend_state"]) != "up":
        return None

    future_idx = df.index[df["date"] > signal_date]
    next_trade_date: object = ""
    next_open: object = pd.NA
    next_open_gap: object = pd.NA
    next_open_status = "待确认"
    if len(future_idx) > 0:
        buy_idx = int(future_idx[0])
        next_open_float = float(df.loc[buy_idx, "open"])
        next_trade_date = pd.Timestamp(df.loc[buy_idx, "date"]).strftime("%Y-%m-%d")
        next_open = next_open_float
        next_open_gap = (next_open_float / close_price - 1.0) * 100.0
        next_open_status = "可买" if next_open_float >= KEY_LEVEL else "不可买"

    code = normalize_code(path.stem)
    volume_expanded = prior_volume_expanded(df, index)
    return {
        "股票代码": code,
        "股票名称": name,
        "日期": signal_date,
        "事件类型": "first_breakout_event",
        "关键价位": KEY_LEVEL,
        "趋势状态": row["trend_state"],
        "收盘确认突破": True,
        "是否首次突破": True,
        "开盘价": float(row["open"]),
        "最高价": float(row["high"]),
        "最低价": float(row["low"]),
        "收盘价": close_price,
        "前收盘价": previous_close,
        "D0涨幅%": (close_price / previous_close - 1.0) * 100.0,
        "突破幅度%": (close_price / KEY_LEVEL - 1.0) * 100.0,
        "成交量放大": volume_expanded,
        "成交量放大_bool": bool(volume_expanded) if volume_expanded is not None else False,
        "次日交易日": next_trade_date,
        "次日开盘": next_open,
        "次日开盘相对D0收盘%": next_open_gap,
        "次日开盘状态": next_open_status,
    }


def build_candidates(args: argparse.Namespace) -> pd.DataFrame:
    if not PARQUET_DIR.exists():
        raise FileNotFoundError(f"Parquet directory not found: {PARQUET_DIR}")

    signal_date = pd.Timestamp(args.date)
    name_map = load_stock_name_map()
    rows: list[dict[str, object]] = []
    paths = sorted(PARQUET_DIR.glob("*.parquet"))
    if args.limit > 0:
        paths = paths[: args.limit]

    tasks = []
    for path in paths:
        code = normalize_code(path.stem)
        name = name_map.get(code, "")
        tasks.append((path, name))

    workers = max(1, int(args.workers))
    if workers == 1:
        for path, name in tasks:
            try:
                row = candidate_from_stock(path, name, signal_date, args.include_st)
            except Exception as exc:
                if args.verbose:
                    print(f"skip {normalize_code(path.stem)}: {type(exc).__name__}: {exc}")
                continue
            if row is not None:
                rows.append(row)
        return pd.DataFrame(rows)

    with ProcessPoolExecutor(max_workers=workers) as executor:
        futures = [
            executor.submit(candidate_from_stock, path, name, signal_date, args.include_st)
            for path, name in tasks
        ]
        for future in as_completed(futures):
            try:
                row = future.result()
            except Exception as exc:
                if args.verbose:
                    print(f"skip worker: {type(exc).__name__}: {exc}")
                continue
            if row is not None:
                rows.append(row)

    return pd.DataFrame(rows)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="快速导出指定日期的利弗莫尔50关键点首次突破候选。")
    parser.add_argument("--date", default=date.today().strftime("%Y-%m-%d"), help="信号日，格式 YYYY-MM-DD")
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    parser.add_argument("--top-n", type=int, default=20)
    parser.add_argument("--include-st", action="store_true")
    parser.add_argument("--limit", type=int, default=0, help="仅用于测试：限制扫描 parquet 文件数量")
    parser.add_argument("--workers", type=int, default=8, help="扫描 worker 数，默认 8")
    parser.add_argument("--verbose", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    candidates = build_candidates(args)
    ranked = score_candidates(candidates) if not candidates.empty else candidates

    args.out_dir.mkdir(parents=True, exist_ok=True)
    out_csv = args.out_dir / f"key_level_50_breakout_candidate_{args.date}.csv"
    if ranked.empty:
        ranked.to_csv(out_csv, index=False, encoding="utf-8-sig")
    else:
        ranked[output_columns(ranked)].to_csv(out_csv, index=False, encoding="utf-8-sig", float_format="%.4f")

    print(f"信号日期: {args.date}")
    print("策略: 利弗莫尔50关键点首次突破；快速扫描本地 parquet；趋势=up")
    print("买入: 下一交易日开盘必须仍在50以上；若本地无次日行情，则开盘前人工确认")
    print("卖出: 跌破50卖出；15%止盈；从最高价回撤7%保护；最多持有10个交易日")
    print(f"候选数: {len(ranked)}")

    if ranked.empty:
        print("结论: 当日无符合规则股票，不买。")
    else:
        print_ranked_preview(ranked, args.top_n)
        first = ranked.iloc[0]
        print()
        if first["是否可买"] == "否":
            print("结论: 排名第一的次日开盘低于50，不买。")
        elif first["是否可买"] == "待确认":
            print(
                f"预选第一名: {first['股票代码']} {first['股票名称']}。"
                "次日开盘数据未确认，开盘必须 >=50 才买。"
            )
        else:
            print(
                f"最符合: {first['股票代码']} {first['股票名称']}，"
                f"次日开盘 {float(first['次日开盘']):.2f} >= 50，可作为买入候选。"
            )
    print(f"已保存: {out_csv.resolve()}")


if __name__ == "__main__":
    main()
