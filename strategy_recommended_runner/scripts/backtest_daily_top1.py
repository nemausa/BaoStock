"""
基于存储的 rebound_top3_ranking_today.csv 回测 top1 策略。

信号来源：每日 daily_replay/<year>/<MM>/<date>_rebound_top3/rebound_top3_ranking_today.csv
的第一行（已过 latest_turn > 5% 过滤），与 simulate_rebound_top3.py 不同（后者不过换手率）。

交易执行逻辑完全复用 simulate_rebound_top3.py：
  - 次日开盘买入，低开 > 2% 跳过
  - 止损 -4%，止盈 +10%，最长持仓 15 个交易日
"""
from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd

from filter_drawdown_rebound_history import PARQUET_DIR, normalize_code
from simulate_rebound_top3 import (
    OpenPosition,
    PendingSignal,
    SkippedSignal,
    TradeRecord,
    build_ohlc_map,
    build_skipped_frame,
    build_summary_frame,
    build_trade_detail_frame,
    check_exit,
    fill_cumulative,
    get_ohlc_row,
    record_open_position,
    write_outputs,
)


DAILY_REPLAY_DIR = Path("a_stock_data/verification_records/daily_replay")
DEFAULT_OUTPUT = Path(
    "strategy_recommended_runner/outputs/2026/backtest_daily_top1_jan_may_2026.xlsx"
)


def load_signal_map(
    daily_dir: Path,
    year: str,
    start_date: pd.Timestamp,
    end_date: pd.Timestamp,
    suffix: str = "rebound_top3",
) -> dict[pd.Timestamp, PendingSignal]:
    """读取每日 rebound_top3_ranking_today.csv，构建信号字典。"""
    signal_map: dict[pd.Timestamp, PendingSignal] = {}
    pattern = f"*_{suffix}/rebound_top3_ranking_today.csv"
    for csv_path in sorted((daily_dir / year).rglob(pattern)):
        date_str = csv_path.parent.name[:10]
        try:
            signal_date = pd.Timestamp(date_str)
        except Exception:
            continue
        if signal_date < start_date or signal_date > end_date:
            continue
        try:
            df = pd.read_csv(csv_path, dtype={"code": str})
        except Exception:
            continue
        if df.empty:
            continue
        row = df.iloc[0]
        code = normalize_code(str(row["code"]))
        signal_map[signal_date] = PendingSignal(
            code=code,
            name=str(row.get("name", "")),
            signal_date=signal_date,
            signal_rank=1,
            rank_score=float(row.get("rank_score", 0.0)),
            signal_close=float(row.get("latest_close", 0.0)),
        )
    return signal_map


def all_trading_days(
    signal_map: dict[pd.Timestamp, PendingSignal],
    ohlc_map: dict[str, pd.DataFrame],
    start_date: pd.Timestamp,
    end_date: pd.Timestamp,
) -> list[pd.Timestamp]:
    """从 OHLC 数据中推导交易日列表（覆盖信号日和潜在持仓日）。"""
    dates: set[pd.Timestamp] = set()
    for df in ohlc_map.values():
        mask = (df["date"] >= start_date) & (df["date"] <= end_date)
        dates.update(df.loc[mask, "date"].tolist())
    return sorted(dates)


def run_simulation(
    signal_map: dict[pd.Timestamp, PendingSignal],
    ohlc_map: dict[str, pd.DataFrame],
    trading_days: list[pd.Timestamp],
    args: argparse.Namespace,
) -> tuple[list[TradeRecord], list[SkippedSignal]]:
    pending_buy: PendingSignal | None = None
    open_position: OpenPosition | None = None
    trades: list[TradeRecord] = []
    skipped: list[SkippedSignal] = []

    for day in trading_days:

        # Step 1: 尝试用昨日信号建仓
        if pending_buy is not None:
            if open_position is None:
                row = get_ohlc_row(ohlc_map, pending_buy.code, day)
                if row is None:
                    skipped.append(SkippedSignal(
                        date=day,
                        code=pending_buy.code,
                        name=pending_buy.name,
                        signal_date=pending_buy.signal_date,
                        rank_score=pending_buy.rank_score,
                        planned_entry_price=float("nan"),
                        signal_close=pending_buy.signal_close,
                        skip_reason="无数据(停牌)",
                    ))
                else:
                    open_p = float(row["open"])
                    prev_close = pending_buy.signal_close
                    gap_ok = prev_close > 0 and open_p >= prev_close * args.gap_down_threshold
                    if gap_ok:
                        stop = open_p * (1 - args.stop_loss_pct)
                        tp = open_p * (1 + args.take_profit_pct)
                        open_position = OpenPosition(
                            code=pending_buy.code,
                            name=pending_buy.name,
                            signal_date=pending_buy.signal_date,
                            signal_rank=pending_buy.signal_rank,
                            rank_score=pending_buy.rank_score,
                            entry_date=day,
                            entry_price=open_p,
                            stop_price=stop,
                            tp_price=tp,
                            holding_days=0,
                        )
                        trade = check_exit(
                            open_position, row, day, args,
                            seq=len(trades) + 1,
                            position_size=args.position_size,
                        )
                        if trade is not None:
                            trades.append(trade)
                            open_position = None
                    else:
                        skipped.append(SkippedSignal(
                            date=day,
                            code=pending_buy.code,
                            name=pending_buy.name,
                            signal_date=pending_buy.signal_date,
                            rank_score=pending_buy.rank_score,
                            planned_entry_price=open_p,
                            signal_close=prev_close,
                            skip_reason="跳空低开(>2%)",
                        ))
            else:
                skipped.append(SkippedSignal(
                    date=day,
                    code=pending_buy.code,
                    name=pending_buy.name,
                    signal_date=pending_buy.signal_date,
                    rank_score=pending_buy.rank_score,
                    planned_entry_price=float("nan"),
                    signal_close=pending_buy.signal_close,
                    skip_reason="已持仓",
                ))
            pending_buy = None

        # Step 2: 检查当前持仓出场（非建仓日）
        if open_position is not None and open_position.entry_date != day:
            row = get_ohlc_row(ohlc_map, open_position.code, day)
            if row is not None:
                open_position.holding_days += 1
                trade = check_exit(
                    open_position, row, day, args,
                    seq=len(trades) + 1,
                    position_size=args.position_size,
                )
                if trade is not None:
                    trades.append(trade)
                    open_position = None

        # Step 3: 空仓且无待买时，取今日 top1 信号
        if open_position is None and pending_buy is None:
            signal = signal_map.get(day)
            if signal is not None:
                pending_buy = signal

        if args.progress:
            if open_position:
                status = f"持仓:{open_position.code}({open_position.holding_days}日)"
            elif pending_buy:
                status = f"待买:{pending_buy.code}"
            else:
                status = "空仓(无信号)"
            print(f"  {day:%Y-%m-%d} {status} | 已成交{len(trades)}笔")

    if open_position is not None and trading_days:
        trade = record_open_position(
            open_position, ohlc_map, trading_days[-1],
            seq=len(trades) + 1,
            position_size=args.position_size,
        )
        trades.append(trade)

    return trades, skipped


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="基于 daily_replay 存储的 top1 CSV 回测 rebound_top3 策略"
    )
    parser.add_argument("--start-date", default="2026-01-05", help="回测开始日期")
    parser.add_argument("--end-date", default="2026-05-29", help="回测结束日期")
    parser.add_argument("--daily-dir", type=Path, default=DAILY_REPLAY_DIR)
    parser.add_argument("--year", default="2026", help="快照年份子目录，默认 2026")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--position-size", type=float, default=100_000.0)
    parser.add_argument("--stop-loss-pct", type=float, default=0.04)
    parser.add_argument("--take-profit-pct", type=float, default=0.10)
    parser.add_argument("--max-hold-days", type=int, default=15)
    parser.add_argument("--gap-down-threshold", type=float, default=0.98)
    parser.add_argument("--include-st", action="store_true")
    parser.add_argument("--progress", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    start = pd.Timestamp(args.start_date)
    end = pd.Timestamp(args.end_date)

    print("读取存储的 top1 信号...")
    signal_map = load_signal_map(args.daily_dir, args.year, start, end)
    print(f"  共 {len(signal_map)} 个交易日有 top1 信号")
    if signal_map:
        first_day = min(signal_map)
        first = signal_map[first_day]
        print(f"  首个信号: {first_day:%Y-%m-%d} → {first.code} {first.name} (rank_score={first.rank_score:.2f})")

    print("加载 OHLC 行情（用于交易执行）...")
    ohlc_map = build_ohlc_map(args)
    print(f"  已加载 {len(ohlc_map)} 只股票 OHLC")

    trading_days = all_trading_days(signal_map, ohlc_map, start, end)
    print(f"  共 {len(trading_days)} 个交易日（{trading_days[0]:%Y-%m-%d} ~ {trading_days[-1]:%Y-%m-%d}）")

    print("开始逐日模拟...")
    trades, skipped = run_simulation(signal_map, ohlc_map, trading_days, args)
    trades = fill_cumulative(trades, args.position_size)

    detail_df = build_trade_detail_frame(trades)
    summary_df = build_summary_frame(trades, args)
    skipped_df = build_skipped_frame(skipped)

    write_outputs(detail_df, summary_df, skipped_df, args)

    print(f"\n=== 回测完成: {len(trades)} 笔交易，{len(skipped)} 个跳过信号 ===")
    if not detail_df.empty:
        cols = ["买入日期", "股票代码", "股票名称", "买入价格", "卖出价格",
                "持仓天数", "退出原因", "盈亏%", "累计收益率%"]
        print(detail_df[[c for c in cols if c in detail_df.columns]].to_string(index=False))

    if not summary_df.empty:
        print("\n=== 汇总 ===")
        print(summary_df.to_string(index=False))


if __name__ == "__main__":
    main()
