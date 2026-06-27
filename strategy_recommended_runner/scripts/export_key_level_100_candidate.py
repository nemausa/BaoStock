from __future__ import annotations

import argparse
from datetime import date
from pathlib import Path

import pandas as pd

from backtest_key_level_trailing_stop import DEFAULT_SIGNALS_FILE, normalize_code


DEFAULT_OUT_DIR = Path("strategy_recommended_runner") / "outputs" / "livermore_key_levels"


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


def score_candidates(candidates: pd.DataFrame, gain_max: float, preferred_close_min: float) -> pd.DataFrame:
    scored = candidates.copy()
    scored["评分"] = 0
    scored.loc[scored["成交量放大_bool"], "评分"] += 2
    scored.loc[scored["D0涨幅%"].ge(10.0) & scored["D0涨幅%"].lt(gain_max), "评分"] += 2
    scored.loc[scored["收盘价"].ge(preferred_close_min), "评分"] += 1
    scored["距100元"] = 100.0 - scored["收盘价"]
    return scored.sort_values(["评分", "收盘价", "股票代码"], ascending=[False, False, True]).reset_index(drop=True)


def select_candidates(signals: pd.DataFrame, args: argparse.Namespace) -> pd.DataFrame:
    signal_date = pd.Timestamp(args.date)
    candidates = signals[
        signals["日期"].eq(signal_date)
        & signals["关键价位"].eq(args.key_level)
        & signals["趋势状态"].eq(args.trend_state)
        & signals["收盘价"].ge(args.close_min)
        & signals["收盘价"].lt(args.close_max)
        & signals["D0涨幅%"].ge(args.gain_min)
        & signals["D0涨幅%"].lt(args.gain_max)
        & signals["最高价"].lt(args.max_signal_high)
    ].copy()

    if args.volume_only:
        candidates = candidates[candidates["成交量放大_bool"]].copy()

    if candidates.empty:
        return candidates

    ranked = score_candidates(candidates, args.gain_max, args.preferred_close_min)
    ranked.insert(0, "候选排名", range(1, len(ranked) + 1))
    return ranked


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="按100元关键价位策略导出指定信号日候选。")
    parser.add_argument("--date", default=date.today().strftime("%Y-%m-%d"), help="信号日期，格式 YYYY-MM-DD")
    parser.add_argument("--signals-file", type=Path, default=DEFAULT_SIGNALS_FILE)
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    parser.add_argument("--key-level", type=float, default=100.0)
    parser.add_argument("--trend-state", default="up")
    parser.add_argument("--close-min", type=float, default=90.0)
    parser.add_argument("--close-max", type=float, default=95.0)
    parser.add_argument("--gain-min", type=float, default=7.0)
    parser.add_argument("--gain-max", type=float, default=20.0)
    parser.add_argument("--max-signal-high", type=float, default=100.0)
    parser.add_argument("--preferred-close-min", type=float, default=92.5)
    parser.add_argument("--volume-only", action="store_true")
    parser.add_argument("--top-n", type=int, default=10)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    signals = load_signals(args.signals_file)
    data_dates = signals["日期"].dropna()
    data_max = data_dates.max()
    signal_date = pd.Timestamp(args.date)

    if signal_date not in set(data_dates.unique()):
        print(f"信号日期: {args.date}")
        print(f"本地 signals 最新日期: {data_max:%Y-%m-%d}")
        print("没有该日期的信号数据。请先更新行情并重建 livermore key level signals。")
        return

    ranked = select_candidates(signals, args)
    args.out_dir.mkdir(parents=True, exist_ok=True)
    out_csv = args.out_dir / f"key_level_100_candidate_{args.date}.csv"
    ranked.to_csv(out_csv, index=False, encoding="utf-8-sig", float_format="%.4f")

    print(f"信号日期: {args.date}")
    print("策略: 100元关键价位；趋势=up；D0收盘90-95；D0涨幅7%-20%；D0最高价<100；不强制放量")
    print("出场: 止损8%；止盈15%；最长持有15个交易日")
    print("买入: 下一交易日开盘买第1名；若你已有持仓，则等卖出后再按1-10自然日等待规则寻找下一信号")
    print(f"候选数: {len(ranked)}")
    if ranked.empty:
        print("结论: 当日无符合规则股票，不买。")
    else:
        preview_columns = [
            "候选排名",
            "股票代码",
            "股票名称",
            "评分",
            "收盘价",
            "最高价",
            "前收盘价",
            "D0涨幅%",
            "成交量放大",
            "趋势状态",
        ]
        print(ranked[[c for c in preview_columns if c in ranked.columns]].head(args.top_n).to_string(index=False))
        first = ranked.iloc[0]
        print()
        print(f"最符合: {first['股票代码']} {first['股票名称']}，下个交易日开盘买入候选。")
    print(f"已保存: {out_csv.resolve()}")


if __name__ == "__main__":
    main()
