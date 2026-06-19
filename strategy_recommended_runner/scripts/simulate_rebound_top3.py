from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pandas as pd

from backtest_rebound_top3 import (
    StockData,
    load_stock_data,
    replay_day,
    replay_dates,
)
from filter_drawdown_rebound_history import (
    PARQUET_DIR,
    is_st_stock,
    load_stock_name_map,
    normalize_code,
)
from rank_rebound_candidates import safe_float


DEFAULT_OUTPUT = Path("strategy_recommended_runner/outputs/simulate_rebound_top3_jan_may_2026.xlsx")


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class PendingSignal:
    code: str
    name: str
    signal_date: pd.Timestamp
    signal_rank: int
    rank_score: float
    signal_close: float


@dataclass
class OpenPosition:
    code: str
    name: str
    signal_date: pd.Timestamp
    signal_rank: int
    rank_score: float
    entry_date: pd.Timestamp
    entry_price: float
    stop_price: float
    tp_price: float
    holding_days: int = 0


@dataclass
class TradeRecord:
    seq: int
    signal_date: Any
    signal_rank: int
    rank_score: float
    code: str
    name: str
    entry_date: Any
    entry_price: float
    stop_price: float
    tp_price: float
    exit_date: Any
    exit_price: float
    holding_days: int
    exit_reason: str
    pnl_pct: float
    pnl_amount: float
    cumulative_pnl: float = 0.0
    cumulative_return_pct: float = 0.0


@dataclass
class SkippedSignal:
    date: Any
    code: str
    name: str
    signal_date: Any
    rank_score: float
    planned_entry_price: float
    signal_close: float
    skip_reason: str


# ---------------------------------------------------------------------------
# OHLC loading (includes open column for trade execution)
# ---------------------------------------------------------------------------

def load_ohlc_with_open(path: Path) -> pd.DataFrame:
    columns = ["date", "open", "high", "low", "close", "pct_chg", "turn"]
    df = pd.read_parquet(path, columns=columns)
    if df.empty:
        return df
    df = df.copy()
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    for col in ["open", "high", "low", "close", "pct_chg", "turn"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.dropna(subset=["date", "open", "high", "low", "close"])
    return df.sort_values("date").reset_index(drop=True)


def build_ohlc_map(args: argparse.Namespace) -> dict[str, pd.DataFrame]:
    name_map = load_stock_name_map()
    ohlc_map: dict[str, pd.DataFrame] = {}
    for path in sorted(PARQUET_DIR.glob("*.parquet")):
        code = normalize_code(path.stem)
        name = name_map.get(code, "")
        if not args.include_st and is_st_stock(name):
            continue
        try:
            df = load_ohlc_with_open(path)
            if not df.empty:
                ohlc_map[code] = df
        except Exception:
            pass
    return ohlc_map


def get_ohlc_row(
    ohlc_map: dict[str, pd.DataFrame],
    code: str,
    date: pd.Timestamp,
) -> pd.Series | None:
    df = ohlc_map.get(normalize_code(code))
    if df is None:
        return None
    rows = df[df["date"] == date]
    if rows.empty:
        return None
    return rows.iloc[-1]


# ---------------------------------------------------------------------------
# Exit logic
# ---------------------------------------------------------------------------

def check_exit(
    position: OpenPosition,
    row: pd.Series,
    date: pd.Timestamp,
    args: argparse.Namespace,
    seq: int,
    position_size: float,
) -> TradeRecord | None:
    entry = position.entry_price
    stop = position.stop_price
    tp = position.tp_price
    open_p = float(row["open"])
    low_p = float(row["low"])
    high_p = float(row["high"])
    close_p = float(row["close"])

    exit_price: float | None = None
    reason: str | None = None

    if open_p <= stop:
        exit_price = open_p
        reason = "止损(跳空低开)"
    elif open_p >= tp:
        exit_price = open_p
        reason = "止盈(跳空高开)"
    elif low_p <= stop:
        exit_price = stop
        reason = f"止损(-{args.stop_loss_pct * 100:.0f}%)"
    elif high_p >= tp:
        exit_price = tp
        reason = f"止盈(+{args.take_profit_pct * 100:.0f}%)"
    elif position.holding_days >= args.max_hold_days:
        exit_price = close_p
        reason = f"到期平仓({args.max_hold_days}日)"

    if exit_price is None:
        return None

    pnl_pct = (exit_price - entry) / entry * 100
    pnl_amount = position_size * (exit_price - entry) / entry
    return TradeRecord(
        seq=seq,
        signal_date=position.signal_date,
        signal_rank=position.signal_rank,
        rank_score=position.rank_score,
        code=position.code,
        name=position.name,
        entry_date=position.entry_date,
        entry_price=entry,
        stop_price=stop,
        tp_price=tp,
        exit_date=date,
        exit_price=exit_price,
        holding_days=position.holding_days,
        exit_reason=reason,
        pnl_pct=pnl_pct,
        pnl_amount=pnl_amount,
    )


def record_open_position(
    position: OpenPosition,
    ohlc_map: dict[str, pd.DataFrame],
    last_date: pd.Timestamp,
    seq: int,
    position_size: float,
) -> TradeRecord:
    row = get_ohlc_row(ohlc_map, position.code, last_date)
    last_price = float(row["close"]) if row is not None else position.entry_price
    pnl_pct = (last_price - position.entry_price) / position.entry_price * 100
    pnl_amount = position_size * (last_price - position.entry_price) / position.entry_price
    return TradeRecord(
        seq=seq,
        signal_date=position.signal_date,
        signal_rank=position.signal_rank,
        rank_score=position.rank_score,
        code=position.code,
        name=position.name,
        entry_date=position.entry_date,
        entry_price=position.entry_price,
        stop_price=position.stop_price,
        tp_price=position.tp_price,
        exit_date="持仓中",
        exit_price=last_price,
        holding_days=position.holding_days,
        exit_reason="持仓中",
        pnl_pct=pnl_pct,
        pnl_amount=pnl_amount,
    )


# ---------------------------------------------------------------------------
# Main simulation loop
# ---------------------------------------------------------------------------

def run_simulation(
    stocks: list[StockData],
    ohlc_map: dict[str, pd.DataFrame],
    trading_days: list[pd.Timestamp],
    args: argparse.Namespace,
) -> tuple[list[TradeRecord], list[SkippedSignal]]:
    pending_buy: PendingSignal | None = None
    open_position: OpenPosition | None = None
    trades: list[TradeRecord] = []
    skipped: list[SkippedSignal] = []

    for day in trading_days:

        # Step 1: Attempt to enter on the pending signal from yesterday
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
                        # Check same-day exit after buying at open
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

        # Step 2: Check exit for open position (not the entry day)
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

        # Step 3: Generate today's signal if free
        if open_position is None and pending_buy is None:
            try:
                ranking = replay_day(stocks, day, args)
            except Exception as exc:
                if args.verbose_errors:
                    print(f"  replay_day 异常 {day:%Y-%m-%d}: {exc}")
                ranking = pd.DataFrame()

            if not ranking.empty:
                top1 = ranking.iloc[0]
                pending_buy = PendingSignal(
                    code=normalize_code(str(top1["code"])),
                    name=str(top1["name"]),
                    signal_date=day,
                    signal_rank=int(top1["rank"]),
                    rank_score=safe_float(top1["rank_score"]),
                    signal_close=safe_float(top1["latest_close"]),
                )

        if args.progress:
            if open_position:
                status = f"持仓:{open_position.code}({open_position.holding_days}日)"
            elif pending_buy:
                status = f"待买:{pending_buy.code}"
            else:
                status = "空仓"
            print(f"  {day:%Y-%m-%d} {status} | 已成交{len(trades)}笔")

    # After loop: record any still-open position
    if open_position is not None and trading_days:
        trade = record_open_position(
            open_position, ohlc_map, trading_days[-1],
            seq=len(trades) + 1,
            position_size=args.position_size,
        )
        trades.append(trade)

    return trades, skipped


# ---------------------------------------------------------------------------
# Fill cumulative columns
# ---------------------------------------------------------------------------

def fill_cumulative(trades: list[TradeRecord], position_size: float) -> list[TradeRecord]:
    running = 0.0
    for t in trades:
        running += t.pnl_amount
        t.cumulative_pnl = running
        t.cumulative_return_pct = running / position_size * 100
    return trades


# ---------------------------------------------------------------------------
# Build output DataFrames
# ---------------------------------------------------------------------------

def _fmt_date(d: Any) -> str:
    if isinstance(d, pd.Timestamp):
        return d.strftime("%Y-%m-%d")
    return str(d)


def build_trade_detail_frame(trades: list[TradeRecord]) -> pd.DataFrame:
    rows = []
    for t in trades:
        rows.append({
            "序号": t.seq,
            "信号日期": _fmt_date(t.signal_date),
            "信号排名": t.signal_rank,
            "rank_score": round(t.rank_score, 4),
            "股票代码": t.code,
            "股票名称": t.name,
            "买入日期": _fmt_date(t.entry_date),
            "买入价格": round(t.entry_price, 3),
            "止损价": round(t.stop_price, 3),
            "止盈价": round(t.tp_price, 3),
            "卖出日期": _fmt_date(t.exit_date),
            "卖出价格": round(t.exit_price, 3) if t.exit_price is not None else "",
            "持仓天数": t.holding_days,
            "退出原因": t.exit_reason,
            "盈亏%": round(t.pnl_pct, 2),
            "盈亏金额(元)": round(t.pnl_amount, 2),
            "累计盈亏(元)": round(t.cumulative_pnl, 2),
            "累计收益率%": round(t.cumulative_return_pct, 2),
        })
    return pd.DataFrame(rows)


def build_summary_frame(trades: list[TradeRecord], args: argparse.Namespace) -> pd.DataFrame:
    closed = [t for t in trades if t.exit_reason != "持仓中"]
    open_pos = [t for t in trades if t.exit_reason == "持仓中"]
    tp_trades = [t for t in closed if "止盈" in t.exit_reason]
    sl_trades = [t for t in closed if "止损" in t.exit_reason]
    expire_trades = [t for t in closed if "到期" in t.exit_reason]

    win_rate = len(tp_trades) / len(closed) * 100 if closed else 0.0
    avg_pnl = sum(t.pnl_pct for t in closed) / len(closed) if closed else 0.0
    max_gain = max((t.pnl_pct for t in closed), default=0.0)
    max_loss = min((t.pnl_pct for t in closed), default=0.0)
    total_pnl = trades[-1].cumulative_pnl if trades else 0.0
    total_return = trades[-1].cumulative_return_pct if trades else 0.0

    rows = [
        ("回测区间", f"{args.start_date} ~ {args.end_date}"),
        ("每笔仓位(元)", f"{args.position_size:,.0f}"),
        ("总交易笔数", len(trades)),
        ("已平仓笔数", len(closed)),
        ("止盈笔数", len(tp_trades)),
        ("止损笔数", len(sl_trades)),
        ("到期平仓笔数", len(expire_trades)),
        ("期末持仓", open_pos[0].code if open_pos else "无"),
        ("胜率%", f"{win_rate:.1f}"),
        ("平均盈亏%", f"{avg_pnl:.2f}"),
        ("最大单笔盈利%", f"{max_gain:.2f}"),
        ("最大单笔亏损%", f"{max_loss:.2f}"),
        ("总盈亏金额(元)", f"{total_pnl:,.2f}"),
        ("总收益率%(基于每笔仓位)", f"{total_return:.2f}"),
        ("止损比例", f"-{args.stop_loss_pct * 100:.0f}%"),
        ("止盈比例", f"+{args.take_profit_pct * 100:.0f}%"),
        ("最大持仓天数", args.max_hold_days),
        ("跳空低开过滤阈值", f"{args.gap_down_threshold * 100:.0f}%"),
    ]
    return pd.DataFrame(rows, columns=["项目", "值"])


def build_skipped_frame(skipped: list[SkippedSignal]) -> pd.DataFrame:
    rows = [{
        "跳过日期": _fmt_date(s.date),
        "股票代码": s.code,
        "股票名称": s.name,
        "信号日期": _fmt_date(s.signal_date),
        "rank_score": round(s.rank_score, 4),
        "计划买入价(开盘)": round(s.planned_entry_price, 3) if not (s.planned_entry_price != s.planned_entry_price) else "",
        "信号收盘价": round(s.signal_close, 3),
        "跳过原因": s.skip_reason,
    } for s in skipped]
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def write_outputs(
    detail_df: pd.DataFrame,
    summary_df: pd.DataFrame,
    skipped_df: pd.DataFrame,
    args: argparse.Namespace,
) -> None:
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with pd.ExcelWriter(args.output, engine="openpyxl") as writer:
        detail_df.to_excel(writer, index=False, sheet_name="交易明细")
        summary_df.to_excel(writer, index=False, sheet_name="汇总")
        skipped_df.to_excel(writer, index=False, sheet_name="跳过信号")

    csv_path = args.output.with_suffix(".csv")
    detail_df.to_csv(csv_path, index=False, encoding="utf-8-sig")
    print(f"Excel: {args.output.resolve()}")
    print(f"CSV:   {csv_path.resolve()}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="模拟 rebound_top3 策略实盘交易 P&L (默认 2026年1-5月)"
    )
    parser.add_argument("--start-date", default="2026-01-05", help="模拟开始日期")
    parser.add_argument("--end-date", default="2026-05-29", help="模拟结束日期")
    parser.add_argument("--position-size", type=float, default=100_000.0,
                        help="每笔仓位(元)，默认 100000")
    parser.add_argument("--stop-loss-pct", type=float, default=0.04,
                        help="止损比例，默认 0.04 (4%%)")
    parser.add_argument("--take-profit-pct", type=float, default=0.10,
                        help="止盈比例，默认 0.10 (10%%)")
    parser.add_argument("--max-hold-days", type=int, default=15,
                        help="最大持仓天数，默认 15")
    parser.add_argument("--gap-down-threshold", type=float, default=0.98,
                        help="跳空低开过滤：开盘价 < 信号收盘 × 此值时放弃入场，默认 0.98")
    # Strategy parameters (passed through to replay_day)
    parser.add_argument("--trend-threshold", type=float, default=0.07)
    parser.add_argument("--drawdown-min", type=float, default=0.27)
    parser.add_argument("--drawdown-max", type=float, default=0.33)
    parser.add_argument("--bottom-area-max", type=float, default=0.05)
    parser.add_argument("--current-rebound-max", type=float, default=0.05)
    parser.add_argument("--full-rebound-min", type=float, default=0.20)
    parser.add_argument("--min-events", type=int, default=1)
    parser.add_argument("--max-neg-pct-chg", type=float, default=-20.0)
    parser.add_argument("--include-st", action="store_true")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--progress", action="store_true", help="逐日打印进度")
    parser.add_argument("--verbose-errors", action="store_true")
    return parser.parse_args()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    args = parse_args()

    print("加载股票数据（用于信号生成）...")
    stocks = load_stock_data(args)
    if not stocks:
        raise RuntimeError("没有可用股票数据")
    print(f"  已加载 {len(stocks)} 只股票")

    print("加载 OHLC 行情（含开盘价，用于交易执行）...")
    ohlc_map = build_ohlc_map(args)
    print(f"  已加载 {len(ohlc_map)} 只股票 OHLC")

    print("确定模拟交易日...")
    trading_days = replay_dates(stocks, args)
    if not trading_days:
        raise RuntimeError(f"区间 {args.start_date}~{args.end_date} 内无交易日数据")
    print(f"  共 {len(trading_days)} 个交易日（{trading_days[0]:%Y-%m-%d} ~ {trading_days[-1]:%Y-%m-%d}）")

    print("开始逐日模拟...")
    trades, skipped = run_simulation(stocks, ohlc_map, trading_days, args)
    trades = fill_cumulative(trades, args.position_size)

    detail_df = build_trade_detail_frame(trades)
    summary_df = build_summary_frame(trades, args)
    skipped_df = build_skipped_frame(skipped)

    write_outputs(detail_df, summary_df, skipped_df, args)

    print(f"\n=== 模拟完成: {len(trades)} 笔交易，{len(skipped)} 个跳过信号 ===")
    if not detail_df.empty:
        cols = ["买入日期", "股票代码", "股票名称", "买入价格", "卖出价格",
                "持仓天数", "退出原因", "盈亏%", "累计收益率%"]
        print(detail_df[cols].to_string(index=False))
    print()
    if not summary_df.empty:
        print(summary_df.to_string(index=False))


if __name__ == "__main__":
    main()
