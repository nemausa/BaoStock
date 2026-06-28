from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd

from backtest_key_level_trailing_stop import normalize_code
from export_key_level_50_breakout_candidate import (
    DEFAULT_EVENTS_FILE,
    DEFAULT_OUT_DIR,
    KEY_LEVEL,
    PARQUET_DIR,
    add_next_open,
    load_events,
    score_candidates,
)


TAKE_PROFIT_PCT = 0.15
TRAILING_DRAWDOWN_PCT = 0.07
MAX_HOLD_DAYS = 10
DEFAULT_PREFIX = "key_level_50_2026_01_returns"


def load_price_frame(code: str) -> pd.DataFrame | None:
    path = PARQUET_DIR / f"{normalize_code(code)}.parquet"
    if not path.exists():
        return None

    df = pd.read_parquet(path).copy()
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    for column in ["open", "high", "low", "close", "volume"]:
        if column in df.columns:
            df[column] = pd.to_numeric(df[column], errors="coerce")
    return (
        df.dropna(subset=["date", "open", "high", "low", "close"])
        .sort_values("date")
        .reset_index(drop=True)
    )


def select_month_candidates(events: pd.DataFrame, args: argparse.Namespace) -> pd.DataFrame:
    start_date = pd.Timestamp(args.start_date)
    end_date = pd.Timestamp(args.end_date)
    candidates = events[
        events["日期"].between(start_date, end_date)
        & events["事件类型"].eq("first_breakout_event")
        & events["关键价位"].eq(KEY_LEVEL)
        & events["趋势状态"].eq(args.trend_state)
        & events["收盘确认突破"].eq(True)
        & events["是否首次突破"].eq(True)
    ].copy()
    if candidates.empty:
        return candidates

    candidates = add_next_open(candidates)
    daily_ranked = []
    for _, group in candidates.groupby("日期", sort=True):
        ranked = score_candidates(group)
        ranked = ranked.rename(columns={"候选排名": "日内排名"})
        daily_ranked.append(ranked)
    return pd.concat(daily_ranked, ignore_index=True).sort_values(
        ["日期", "日内排名", "股票代码"]
    ).reset_index(drop=True)


def exit_trade(df: pd.DataFrame, buy_idx: int, buy_price: float, args: argparse.Namespace) -> dict[str, object]:
    target_price = buy_price * (1.0 + args.take_profit_pct)
    sell_limit_idx = min(buy_idx + args.max_hold_days - 1, len(df) - 1)
    buy_day_high = float(df.loc[buy_idx, "high"])
    peak_price = max(buy_price, buy_day_high)
    sell_idx = sell_limit_idx
    sell_price = float(df.loc[sell_limit_idx, "close"])
    sell_reason = f"持有满{args.max_hold_days}个交易日"

    if buy_idx + 1 > sell_limit_idx:
        return {
            "sell_idx": None,
            "sell_date": pd.NaT,
            "sell_price": pd.NA,
            "sell_reason": "买入后无可卖出交易日",
            "peak_price": peak_price,
            "target_price": target_price,
            "return_pct": pd.NA,
        }

    for idx in range(buy_idx + 1, sell_limit_idx + 1):
        open_price = float(df.loc[idx, "open"])
        high_price = float(df.loc[idx, "high"])
        low_price = float(df.loc[idx, "low"])
        close_price = float(df.loc[idx, "close"])

        if open_price < KEY_LEVEL:
            sell_idx = idx
            sell_price = open_price
            sell_reason = "开盘跌破50关键点"
            break
        if low_price < KEY_LEVEL:
            sell_idx = idx
            sell_price = KEY_LEVEL
            sell_reason = "盘中跌破50关键点"
            break
        if open_price >= target_price:
            sell_idx = idx
            sell_price = open_price
            sell_reason = "开盘达到15%止盈"
            break
        if high_price >= target_price:
            sell_idx = idx
            sell_price = target_price
            sell_reason = "盘中达到15%止盈"
            break

        peak_price = max(peak_price, high_price)
        trailing_stop_price = peak_price * (1.0 - args.trailing_drawdown_pct)
        if peak_price > buy_price and low_price <= trailing_stop_price:
            sell_idx = idx
            sell_price = trailing_stop_price
            sell_reason = "从持仓最高价回撤7%"
            break

        if idx == sell_limit_idx:
            sell_price = close_price

    return {
        "sell_idx": sell_idx,
        "sell_date": pd.Timestamp(df.loc[sell_idx, "date"]),
        "sell_price": sell_price,
        "sell_reason": sell_reason,
        "peak_price": peak_price,
        "target_price": target_price,
        "return_pct": (sell_price / buy_price - 1.0) * 100.0,
    }


def simulate_candidate(signal: pd.Series, args: argparse.Namespace) -> dict[str, object]:
    signal_date = pd.Timestamp(signal["日期"])
    code = normalize_code(signal["股票代码"])
    name = signal["股票名称"]
    base = {
        "信号日": signal_date.strftime("%Y-%m-%d"),
        "股票代码": code,
        "股票名称": name,
        "日内排名": int(signal["日内排名"]),
        "评分": int(signal["评分"]),
        "是否可买": signal.get("是否可买", "待确认"),
        "D0收盘": float(signal["收盘价"]),
        "D0涨幅%": float(signal["D0涨幅%"]),
        "突破幅度%": float(signal["突破幅度%"]),
        "成交量放大": "是" if bool(signal.get("成交量放大_bool", False)) else "否",
        "买入日": "",
        "买入价": pd.NA,
        "目标止盈价": pd.NA,
        "卖出日": "",
        "卖出价": pd.NA,
        "卖出原因": "",
        "持有交易日": pd.NA,
        "持仓最高价": pd.NA,
        "收益率%": pd.NA,
        "交易状态": "未交易",
        "跳过原因": "",
    }

    df = load_price_frame(code)
    if df is None or df.empty:
        base["跳过原因"] = "缺少行情文件"
        return base

    future_idx = df.index[df["date"] > signal_date]
    if len(future_idx) == 0:
        base["跳过原因"] = "无次日行情"
        return base

    buy_idx = int(future_idx[0])
    buy_date = pd.Timestamp(df.loc[buy_idx, "date"])
    buy_price = float(df.loc[buy_idx, "open"])
    base["买入日"] = buy_date.strftime("%Y-%m-%d")
    base["买入价"] = buy_price

    if buy_price < KEY_LEVEL:
        base["跳过原因"] = "次日开盘低于50"
        return base

    exit_result = exit_trade(df, buy_idx, buy_price, args)
    if exit_result["sell_idx"] is None:
        base["目标止盈价"] = float(exit_result["target_price"])
        base["持仓最高价"] = float(exit_result["peak_price"])
        base["跳过原因"] = exit_result["sell_reason"]
        return base

    sell_date = pd.Timestamp(exit_result["sell_date"])
    base.update(
        {
            "目标止盈价": float(exit_result["target_price"]),
            "卖出日": sell_date.strftime("%Y-%m-%d"),
            "卖出价": float(exit_result["sell_price"]),
            "卖出原因": exit_result["sell_reason"],
            "持有交易日": int(exit_result["sell_idx"] - buy_idx + 1),
            "持仓最高价": float(exit_result["peak_price"]),
            "收益率%": float(exit_result["return_pct"]),
            "交易状态": "已完成",
        }
    )
    return base


def simulate_all_candidates(candidates: pd.DataFrame, args: argparse.Namespace) -> pd.DataFrame:
    rows = [simulate_candidate(row, args) for _, row in candidates.iterrows()]
    return pd.DataFrame(rows)


def simulate_single_holding(candidate_returns: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    trades = []
    skipped = []
    position_until = pd.Timestamp.min
    equity = 1.0

    daily_top = candidate_returns[candidate_returns["日内排名"].eq(1)].copy()
    daily_top["信号日_ts"] = pd.to_datetime(daily_top["信号日"])
    daily_top["卖出日_ts"] = pd.to_datetime(daily_top["卖出日"], errors="coerce")

    for _, row in daily_top.sort_values(["信号日_ts", "股票代码"]).iterrows():
        signal_date = pd.Timestamp(row["信号日_ts"])
        if row["交易状态"] != "已完成":
            skipped.append(
                {
                    "信号日": row["信号日"],
                    "股票代码": row["股票代码"],
                    "股票名称": row["股票名称"],
                    "跳过原因": row["跳过原因"] or "未完成交易",
                    "上一笔卖出日": "",
                }
            )
            continue

        if signal_date <= position_until:
            skipped.append(
                {
                    "信号日": row["信号日"],
                    "股票代码": row["股票代码"],
                    "股票名称": row["股票名称"],
                    "跳过原因": "单持仓仍持有上一笔",
                    "上一笔卖出日": position_until.strftime("%Y-%m-%d"),
                }
            )
            continue

        return_pct = float(row["收益率%"])
        equity *= 1.0 + return_pct / 100.0
        out = row.drop(labels=["信号日_ts", "卖出日_ts"]).to_dict()
        out["单持仓累计收益率%"] = (equity - 1.0) * 100.0
        trades.append(out)
        position_until = pd.Timestamp(row["卖出日_ts"])

    return pd.DataFrame(trades), pd.DataFrame(skipped)


def summary_frame(all_returns: pd.DataFrame, single_trades: pd.DataFrame, skipped: pd.DataFrame, args: argparse.Namespace) -> pd.DataFrame:
    completed = all_returns[all_returns["交易状态"].eq("已完成")].copy()
    all_returns_pct = pd.to_numeric(completed.get("收益率%", pd.Series(dtype=float)), errors="coerce")
    single_returns_pct = pd.to_numeric(single_trades.get("收益率%", pd.Series(dtype=float)), errors="coerce")
    single_total = (
        float(single_trades["单持仓累计收益率%"].iloc[-1])
        if not single_trades.empty and "单持仓累计收益率%" in single_trades.columns
        else 0.0
    )
    rows = [
        ("start_date", args.start_date),
        ("end_date", args.end_date),
        ("key_level", KEY_LEVEL),
        ("候选信号数", int(len(all_returns))),
        ("可完成交易信号数", int(len(completed))),
        ("全部候选胜率%", float((all_returns_pct > 0).mean() * 100.0) if len(all_returns_pct) else 0.0),
        ("全部候选平均收益率%", float(all_returns_pct.mean()) if len(all_returns_pct) else 0.0),
        ("全部候选收益率合计%", float(all_returns_pct.sum()) if len(all_returns_pct) else 0.0),
        ("单持仓交易数", int(len(single_trades))),
        ("单持仓跳过数", int(len(skipped))),
        ("单持仓胜率%", float((single_returns_pct > 0).mean() * 100.0) if len(single_returns_pct) else 0.0),
        ("单持仓平均单笔收益率%", float(single_returns_pct.mean()) if len(single_returns_pct) else 0.0),
        ("单持仓累计收益率%", single_total),
        ("最大单笔收益率%", float(single_returns_pct.max()) if len(single_returns_pct) else 0.0),
        ("最大单笔亏损率%", float(single_returns_pct.min()) if len(single_returns_pct) else 0.0),
        ("entry", "signal next trading day open, require open >= 50"),
        ("exit", "break below 50; +15% target; 7% drawdown from holding high; max 10 trading days"),
    ]
    return pd.DataFrame(rows, columns=["项目", "值"])


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="统计2026年1月50关键点首次突破收益和单持仓收益。")
    parser.add_argument("--events-file", type=Path, default=DEFAULT_EVENTS_FILE)
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    parser.add_argument("--prefix", default=DEFAULT_PREFIX)
    parser.add_argument("--start-date", default="2026-01-01")
    parser.add_argument("--end-date", default="2026-01-31")
    parser.add_argument("--trend-state", default="up")
    parser.add_argument("--take-profit-pct", type=float, default=TAKE_PROFIT_PCT)
    parser.add_argument("--trailing-drawdown-pct", type=float, default=TRAILING_DRAWDOWN_PCT)
    parser.add_argument("--max-hold-days", type=int, default=MAX_HOLD_DAYS)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    events = load_events(args.events_file)
    candidates = select_month_candidates(events, args)
    all_returns = simulate_all_candidates(candidates, args)
    single_trades, single_skipped = simulate_single_holding(all_returns)
    summary = summary_frame(all_returns, single_trades, single_skipped, args)

    args.out_dir.mkdir(parents=True, exist_ok=True)
    candidates_file = args.out_dir / f"{args.prefix}_candidates.csv"
    all_returns_file = args.out_dir / f"{args.prefix}_all_returns.csv"
    single_file = args.out_dir / f"{args.prefix}_single_holding_trades.csv"
    skipped_file = args.out_dir / f"{args.prefix}_single_holding_skipped.csv"
    summary_file = args.out_dir / f"{args.prefix}_summary.csv"

    candidates.to_csv(candidates_file, index=False, encoding="utf-8-sig", float_format="%.4f")
    all_returns.to_csv(all_returns_file, index=False, encoding="utf-8-sig", float_format="%.4f")
    single_trades.to_csv(single_file, index=False, encoding="utf-8-sig", float_format="%.4f")
    single_skipped.to_csv(skipped_file, index=False, encoding="utf-8-sig", float_format="%.4f")
    summary.to_csv(summary_file, index=False, encoding="utf-8-sig", float_format="%.4f")

    print("策略: 利弗莫尔50关键点首次突破")
    print("日期: %s 至 %s" % (args.start_date, args.end_date))
    print("卖出: 跌破50；15%止盈；从持仓最高价回撤7%；最多持有10个交易日")
    print()
    print(summary.to_string(index=False))
    if not single_trades.empty:
        print()
        preview_columns = ["信号日", "股票代码", "股票名称", "买入日", "买入价", "卖出日", "卖出价", "卖出原因", "收益率%", "单持仓累计收益率%"]
        print(single_trades[preview_columns].to_string(index=False))
    print()
    print(f"候选: {candidates_file.resolve()}")
    print(f"每个收益: {all_returns_file.resolve()}")
    print(f"单持仓交易: {single_file.resolve()}")
    print(f"单持仓跳过: {skipped_file.resolve()}")
    print(f"汇总: {summary_file.resolve()}")


if __name__ == "__main__":
    main()
