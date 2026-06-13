from __future__ import annotations

import argparse
import copy
import io
import sys
from datetime import date
from pathlib import Path

import pandas as pd


class _Tee:
    """同时写入多个流（控制台 + 字符串缓冲）。"""
    def __init__(self, *streams: io.IOBase) -> None:
        self._streams = streams

    def write(self, data: str) -> None:
        for s in self._streams:
            s.write(data)

    def flush(self) -> None:
        for s in self._streams:
            s.flush()


def format_box_table(headers: list[str], rows: list[list[str]]) -> str:
    """用 ┌┬┐├┼┤└┴┘ 边框字符渲染表格，单元格内容居中。"""
    col_widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            col_widths[i] = max(col_widths[i], len(cell))
    # 每列两侧各留一个空格
    widths = [w + 2 for w in col_widths]

    def _border(left: str, mid: str, right: str, fill: str) -> str:
        return left + mid.join(fill * w for w in widths) + right

    def _row(cells: list[str]) -> str:
        parts = []
        for cell, w in zip(cells, widths):
            inner = w - 2
            parts.append(" " + cell.center(inner) + " ")
        return "│" + "│".join(parts) + "│"

    lines = [
        _border("┌", "┬", "┐", "─"),
        _row(headers),
    ]
    sep = _border("├", "┼", "┤", "─")
    for row in rows:
        lines.append(sep)
        lines.append(_row(row))
    lines.append(_border("└", "┴", "┘", "─"))
    return "\n".join(lines)


DAILY_REPLAY_DIR = Path("a_stock_data/verification_records/daily_replay")
PARQUET_DIR = Path("a_stock_data/parquet")
OUTPUT_DIR = Path("strategy_recommended_runner/outputs")

DEFAULT_PERIODS = [
    ("2021", "2021-01-01", "2021-12-31"),
    ("2022", "2022-01-01", "2022-12-31"),
    ("2023", "2023-01-01", "2023-12-31"),
    ("2024", "2024-01-01", "2024-12-31"),
    ("2025", "2025-01-01", "2025-12-31"),
    ("2026年1-5月", "2026-01-01", "2026-05-31"),
    ("2026-至今", "2026-01-01", date.today().strftime("%Y-%m-%d")),
    ("2021-至今连续", "2021-01-01", date.today().strftime("%Y-%m-%d")),
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="统计 rebound_top3 策略每年收益率。")
    parser.add_argument("--start-year", type=int, default=2021)
    parser.add_argument("--end-year", type=int, default=date.today().year)
    parser.add_argument("--daily-dir", type=Path, default=DAILY_REPLAY_DIR)
    parser.add_argument("--parquet-dir", type=Path, default=PARQUET_DIR)
    parser.add_argument("--min-latest-turn", type=float, default=5.0)
    parser.add_argument("--max-current-drawdown", type=float, default=33.0)
    parser.add_argument("--max-gap-down", type=float, default=-2.0,
                        help="低开过滤：entry_gap_pct 小于此值则跳过，-2.0 表示过滤低开>2%%，None=不过滤")
    parser.add_argument("--min-gap-up", type=float, default=None,
                        help="微高开过滤：0%%~min_gap_up%%之间的高开跳过；None=不过滤（默认）")
    parser.add_argument("--min-rank-score", type=float, default=None,
                        help="rank1 最低分数门槛（rank_score），低于此值则跳过；None=不过滤（默认）")
    parser.add_argument("--min-breadth", type=int, default=2,
                        help="信号广度过滤：当日过滤后候选数 < 此值则全天不交易；1=不过滤（默认2=要求市场共振）")
    parser.add_argument("--take-profit", type=float, default=0.10)
    parser.add_argument("--stop-loss", type=float, default=0.04)
    parser.add_argument("--max-hold-days", type=int, default=15)
    parser.add_argument("--reentry-cooldown-days", type=int, default=10)
    parser.add_argument("--initial-capital", type=float, default=50000.0)
    parser.add_argument("--lot-size", type=int, default=100)
    # 交易手续费（A股，标准·历史精确）
    parser.add_argument("--commission-rate", type=float, default=0.00025, help="佣金费率(双边)，默认万2.5")
    parser.add_argument("--min-commission", type=float, default=5.0, help="单笔最低佣金，默认5元")
    parser.add_argument("--transfer-fee-rate", type=float, default=0.00001, help="过户费率(双边)，默认0.001%")
    parser.add_argument("--stamp-duty-rate", type=float, default=0.0005, help="印花税率(仅卖出)，默认0.05%")
    parser.add_argument("--stamp-duty-rate-old", type=float, default=0.001, help="2023-08-28前印花税率，默认0.1%")
    parser.add_argument("--stamp-duty-cut-date", type=str, default="2023-08-28", help="印花税下调生效日")
    parser.add_argument("--no-fees", action="store_true", help="关闭手续费(回到旧口径对比)")
    parser.add_argument("--output", type=Path, default=OUTPUT_DIR / "annual_backtest_rebound_top3.xlsx")
    return parser.parse_args()


def normalize_code(code: object) -> str:
    text = str(code).strip()
    if "." in text:
        text = text.split(".")[-1]
    return text.zfill(6)


def numeric(df: pd.DataFrame, col: str) -> pd.Series:
    return pd.to_numeric(df[col], errors="coerce") if col in df.columns else pd.Series(dtype=float)


class PriceCache:
    def __init__(self, parquet_dir: Path) -> None:
        self.parquet_dir = parquet_dir
        self._cache: dict[str, pd.DataFrame] = {}

    def get(self, code: str) -> pd.DataFrame:
        code = normalize_code(code)
        if code not in self._cache:
            path = self.parquet_dir / f"{code}.parquet"
            if not path.exists():
                self._cache[code] = pd.DataFrame()
                return self._cache[code]
            df = pd.read_parquet(path, columns=["date", "open", "high", "low", "close", "preclose"])
            df = df.copy()
            df["date"] = pd.to_datetime(df["date"], errors="coerce")
            for col in ["open", "high", "low", "close", "preclose"]:
                df[col] = pd.to_numeric(df[col], errors="coerce")
            df = df.dropna(subset=["date", "open", "high", "low", "close"]).sort_values("date").reset_index(drop=True)
            df["date_str"] = df["date"].dt.strftime("%Y-%m-%d")
            self._cache[code] = df
        return self._cache[code]


def next_entry(price_cache: PriceCache, code: str, signal_date: str) -> dict[str, object] | None:
    df = price_cache.get(code)
    if df.empty:
        return None
    future = df[df["date"] > pd.Timestamp(signal_date)]
    if future.empty:
        return None
    row = future.iloc[0]
    preclose = row.get("preclose")
    if pd.notna(preclose) and float(preclose) > 0:
        gap_pct = round((float(row["open"]) / float(preclose) - 1) * 100, 3)
    else:
        gap_pct = 0.0
    return {
        "entry_index": int(future.index[0]),
        "entry_date": str(row["date_str"]),
        "entry_open": float(row["open"]),
        "entry_gap_pct": gap_pct,
    }


def cooldown_until(price_cache: PriceCache, code: str, exit_date: str, cooldown_days: int) -> str:
    if cooldown_days <= 0:
        return exit_date
    df = price_cache.get(code)
    future = df[df["date"] > pd.Timestamp(exit_date)].head(cooldown_days)
    if future.empty:
        return "9999-12-31"
    return str(future.iloc[-1]["date_str"])


def exit_trade(
    price_cache: PriceCache,
    code: str,
    entry_index: int,
    entry_price: float,
    args: argparse.Namespace,
) -> dict[str, object]:
    df = price_cache.get(code)
    take_profit_price = entry_price * (1 + args.take_profit)
    stop_loss_price = entry_price * (1 - args.stop_loss)
    last_index = min(len(df) - 1, entry_index + args.max_hold_days - 1)

    for idx in range(entry_index + 1, last_index + 1):
        row = df.loc[idx]
        holding_days = idx - entry_index + 1
        open_p = float(row["open"])
        # 跳空低开直接穿破止损/止盈：按开盘价成交
        if open_p <= stop_loss_price:
            return {"exit_date": str(row["date_str"]), "exit_price": open_p, "exit_reason": "止损(低开)", "holding_days": holding_days}
        if open_p >= take_profit_price:
            return {"exit_date": str(row["date_str"]), "exit_price": open_p, "exit_reason": "止盈(高开)", "holding_days": holding_days}
        # 日内触碰
        if float(row["low"]) <= stop_loss_price:
            return {"exit_date": str(row["date_str"]), "exit_price": stop_loss_price, "exit_reason": "止损", "holding_days": holding_days}
        if float(row["high"]) >= take_profit_price:
            return {"exit_date": str(row["date_str"]), "exit_price": take_profit_price, "exit_reason": "止盈", "holding_days": holding_days}
        if holding_days >= args.max_hold_days:
            return {"exit_date": str(row["date_str"]), "exit_price": float(row["close"]), "exit_reason": "到期", "holding_days": holding_days}

    row = df.loc[last_index]
    return {"exit_date": str(row["date_str"]), "exit_price": float(row["close"]), "exit_reason": "到期", "holding_days": last_index - entry_index + 1}


def load_daily_rankings(daily_dir: Path, start_date: str, end_date: str) -> pd.DataFrame:
    frames: list[pd.DataFrame] = []
    for path in sorted(daily_dir.glob("*_rebound_top3/ranking_snapshot.csv")):
        signal_date = path.parent.name[:10]
        if signal_date < start_date or signal_date > end_date:
            continue
        try:
            pd.Timestamp(signal_date)
        except Exception:
            continue
        df = pd.read_csv(path, dtype={"code": str})
        if df.empty or "rank" not in df.columns:
            continue
        df = df.copy()
        df["code"] = df["code"].map(normalize_code)
        df["rank"] = numeric(df, "rank")
        df.insert(0, "signal_date", signal_date)
        frames.append(df.sort_values("rank"))
    if not frames:
        return pd.DataFrame()
    daily = pd.concat(frames, ignore_index=True)
    for col in ["rank_score", "latest_turn", "current_drawdown_pct"]:
        if col in daily.columns:
            daily[col] = numeric(daily, col)
    return daily.sort_values(["signal_date", "rank"]).reset_index(drop=True)


def select_top1(
    daily: pd.DataFrame,
    min_turn: float | None = None,
    max_drawdown: float | None = None,
    min_breadth: int = 1,
) -> pd.DataFrame:
    if daily.empty:
        return daily
    filtered = daily.copy()
    if min_turn is not None:
        filtered = filtered[numeric(filtered, "latest_turn") >= min_turn]
    if max_drawdown is not None:
        filtered = filtered[numeric(filtered, "current_drawdown_pct") <= max_drawdown]
    if filtered.empty:
        return filtered
    # 信号广度过滤：当日过滤后候选数 < min_breadth 的整日剔除（要求市场共振）
    if min_breadth and min_breadth > 1:
        day_count = filtered.groupby("signal_date")["signal_date"].transform("size")
        filtered = filtered[day_count >= min_breadth]
        if filtered.empty:
            return filtered
    return filtered.sort_values(["signal_date", "rank"]).groupby("signal_date", group_keys=False).head(1).reset_index(drop=True)


def select_top_n(
    daily: pd.DataFrame,
    n: int = 3,
    min_turn: float | None = None,
    max_drawdown: float | None = None,
) -> pd.DataFrame:
    if daily.empty:
        return daily
    filtered = daily.copy()
    if min_turn is not None:
        filtered = filtered[numeric(filtered, "latest_turn") >= min_turn]
    if max_drawdown is not None:
        filtered = filtered[numeric(filtered, "current_drawdown_pct") <= max_drawdown]
    if filtered.empty:
        return filtered
    return filtered.sort_values(["signal_date", "rank"]).groupby("signal_date", group_keys=False).head(n).reset_index(drop=True)


def run_combo(
    daily_cache: dict[str, pd.DataFrame],
    price_cache: PriceCache,
    args: argparse.Namespace,
    min_turn: float | None,
    max_drawdown: float | None,
    max_gap_down: float | None = None,
    min_gap_up: float | None = None,
    min_rank_score: float | None = None,
    min_breadth: int | None = None,
) -> dict[str, object]:
    """对所有 period 跑一套 (min_turn, max_drawdown) 组合，返回各年收益率和交易数。"""
    breadth = args.min_breadth if min_breadth is None else min_breadth
    result: dict[str, object] = {}
    total_trades = 0
    for label, daily in daily_cache.items():
        candidates = select_top1(daily, min_turn, max_drawdown, min_breadth=breadth)
        orders = build_orders(candidates, price_cache)
        trades, assets = simulate(orders, price_cache, args,
                                  max_gap_down=max_gap_down, min_gap_up=min_gap_up,
                                  min_rank_score=min_rank_score)
        s = summarize_period(label, trades, assets, args)
        result[label] = f"{s['收益率_pct']:+.1f}% ({int(s['交易数'])}笔)"
        if label != "2021-至今连续":
            total_trades += int(s["交易数"])
    result["交易数(不含连续)"] = total_trades
    return result


def build_orders(candidates: pd.DataFrame, price_cache: PriceCache) -> pd.DataFrame:
    rows: list[dict[str, object]] = []
    for _, row in candidates.iterrows():
        entry = next_entry(price_cache, str(row["code"]), str(row["signal_date"]))
        if entry is None:
            continue
        rows.append({**row.to_dict(), **entry})
    if not rows:
        return pd.DataFrame()
    return pd.DataFrame(rows).sort_values(["entry_date", "signal_date", "rank"]).reset_index(drop=True)


def buy_fee(amount: float, args: argparse.Namespace) -> float:
    """买入手续费：佣金(双边,最低5元) + 过户费(双边)。"""
    if getattr(args, "no_fees", False):
        return 0.0
    commission = max(amount * args.commission_rate, args.min_commission)
    transfer = amount * args.transfer_fee_rate
    return commission + transfer


def sell_fee(amount: float, sell_date: str, args: argparse.Namespace) -> float:
    """卖出手续费：佣金(双边,最低5元) + 过户费(双边) + 印花税(仅卖出,2023-08-28前0.1%后0.05%)。"""
    if getattr(args, "no_fees", False):
        return 0.0
    commission = max(amount * args.commission_rate, args.min_commission)
    transfer = amount * args.transfer_fee_rate
    stamp_rate = args.stamp_duty_rate_old if str(sell_date) < args.stamp_duty_cut_date else args.stamp_duty_rate
    stamp = amount * stamp_rate
    return commission + transfer + stamp


def simulate(
    orders: pd.DataFrame,
    price_cache: PriceCache,
    args: argparse.Namespace,
    max_gap_down: float | None = None,
    min_gap_up: float | None = None,
    min_rank_score: float | None = None,
    decision_log: list | None = None,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    if orders.empty:
        return pd.DataFrame(), pd.DataFrame()

    cash = float(args.initial_capital)
    busy_until = ""
    cooldown: dict[str, str] = {}
    trades: list[dict[str, object]] = []
    asset_marks: list[dict[str, object]] = [{"date": str(orders["entry_date"].min()), "total_asset": cash}]

    for _, order in orders.iterrows():
        entry_date = str(order["entry_date"])
        code = normalize_code(order["code"])

        def _log(action: str, exit_info: dict | None = None) -> None:
            if decision_log is None:
                return
            rec: dict[str, object] = {
                "信号日期(T+0)":  str(order.get("signal_date", "")),
                "候选顺序":       int(order.get("rank", 0)),
                "代码":           code,
                "名称":           str(order.get("name", "")),
                "rank_score":     round(float(order.get("rank_score") or 0), 2),
                "换手率%":        round(float(order.get("latest_turn") or 0), 2),
                "回撤%":          round(float(order.get("current_drawdown_pct") or 0), 2),
                "买入日期(T+1)":  entry_date,
                "开盘价":         round(float(order.get("entry_open") or 0), 3),
                "昨收":           round(float(order.get("preclose") or 0), 3),
                "低开幅度%":      round(float(order.get("entry_gap_pct") or 0), 3),
                "操作":           action,
                "卖出日期":       exit_info["exit_date"] if exit_info else "",
                "卖出价":         exit_info["exit_price"] if exit_info else "",
                "退出原因":       exit_info["exit_reason"] if exit_info else "",
                "收益率%":        exit_info.get("return_pct", "") if exit_info else "",
            }
            decision_log.append(rec)

        if entry_date <= busy_until:
            _log("已持仓")
            continue
        if code in cooldown and entry_date <= cooldown[code]:
            _log("冷却期")
            continue

        gap = float(order.get("entry_gap_pct", 0.0))
        if max_gap_down is not None and gap < max_gap_down:
            _log("低开跳过")
            continue
        if min_gap_up is not None and 0.0 <= gap < min_gap_up:
            _log("微高开跳过")
            continue
        if min_rank_score is not None and float(order.get("rank_score", 0.0)) < min_rank_score:
            _log("分数不足跳过")
            continue

        entry_price = float(order["entry_open"])
        lot_cost = entry_price * int(args.lot_size)
        # 预留买入手续费空间，避免现金穿负（--no-fees 时不预留，便于与旧口径对齐）
        buy_fee_buffer = 1.0 if getattr(args, "no_fees", False) else (1 + args.commission_rate + args.transfer_fee_rate)
        quantity = int(cash // (lot_cost * buy_fee_buffer)) * int(args.lot_size) if lot_cost > 0 else 0
        if quantity < int(args.lot_size):
            continue

        buy_amount = quantity * entry_price
        bfee = buy_fee(buy_amount, args)
        cash_before_buy = cash
        cash_after_entry = cash - buy_amount - bfee          # 买入扣佣金+过户费
        take_profit_price = entry_price * (1 + args.take_profit)
        stop_loss_price = entry_price * (1 - args.stop_loss)

        exit_info = exit_trade(price_cache, code, int(order["entry_index"]), entry_price, args)
        exit_price = float(exit_info["exit_price"])
        sell_amount = quantity * exit_price
        sfee = sell_fee(sell_amount, exit_info["exit_date"], args)   # 卖出扣佣金+印花税+过户费
        total_fee = bfee + sfee
        pnl = (sell_amount - sfee) - (buy_amount + bfee)     # 含双边费用的净盈亏
        cash = cash_after_entry + sell_amount - sfee

        asset_marks.append({"date": exit_info["exit_date"], "total_asset": cash})
        trades.append({
            "signal_date": order.get("signal_date"),
            "rank": order.get("rank"),
            "rank_score": order.get("rank_score"),
            "code": code,
            "name": order.get("name", ""),
            "entry_date": entry_date,
            "entry_price": round(entry_price, 3),
            "quantity": quantity,
            "buy_amount": round(buy_amount, 2),
            "take_profit_price": round(take_profit_price, 3),
            "stop_loss_price": round(stop_loss_price, 3),
            "exit_date": exit_info["exit_date"],
            "exit_price": round(exit_price, 3),
            "sell_amount": round(sell_amount, 2),
            "holding_days": exit_info["holding_days"],
            "exit_reason": exit_info["exit_reason"],
            "buy_fee": round(bfee, 2),
            "sell_fee": round(sfee, 2),
            "total_fee": round(total_fee, 2),
            "pnl": round(pnl, 2),
            "return_pct": round(pnl / (buy_amount + bfee) * 100 if buy_amount else 0.0, 2),
            "cash_before_buy": round(cash_before_buy, 2),
            "cash_after_exit": round(cash, 2),
        })

        busy_until = str(exit_info["exit_date"])
        cooldown[code] = cooldown_until(price_cache, code, str(exit_info["exit_date"]), int(args.reentry_cooldown_days))
        _log("买入", {**exit_info, "return_pct": round(pnl / (buy_amount + bfee) * 100 if buy_amount else 0.0, 2)})

    trades_df = pd.DataFrame(trades)
    assets_df = pd.DataFrame(asset_marks).sort_values("date").reset_index(drop=True)
    return trades_df, assets_df


def summarize_period(label: str, trades: pd.DataFrame, assets: pd.DataFrame, args: argparse.Namespace) -> dict[str, object]:
    initial = float(args.initial_capital)
    final = float(assets["total_asset"].iloc[-1]) if not assets.empty else initial
    total_return = (final - initial) / initial * 100

    if not assets.empty:
        peak = assets["total_asset"].cummax()
        max_dd = ((assets["total_asset"] - peak) / peak * 100).min()
    else:
        max_dd = 0.0

    trade_count = int(len(trades))
    wins = int((trades["pnl"] > 0).sum()) if not trades.empty else 0
    win_rate = wins / trade_count * 100 if trade_count else 0.0

    take_profit = int(trades["exit_reason"].str.startswith("止盈").sum()) if not trades.empty else 0
    stop_loss = int(trades["exit_reason"].str.startswith("止损").sum()) if not trades.empty else 0
    time_exit = int((trades["exit_reason"] == "到期").sum()) if not trades.empty else 0

    return {
        "周期": label,
        "交易数": trade_count,
        "胜率_pct": round(win_rate, 2),
        "止盈数": take_profit,
        "止损数": stop_loss,
        "到期数": time_exit,
        "初始资金": initial,
        "最终资产": round(final, 2),
        "总收益": round(final - initial, 2),
        "收益率_pct": round(total_return, 2),
        "最大回撤_pct": round(float(max_dd), 2),
        "平均单笔收益": round(float(trades["pnl"].mean()), 2) if not trades.empty else 0.0,
        "总手续费": round(float(trades["total_fee"].sum()), 2) if (not trades.empty and "total_fee" in trades.columns) else 0.0,
    }


def main() -> None:
    args = parse_args()
    price_cache = PriceCache(args.parquet_dir)

    periods = [p for p in DEFAULT_PERIODS if p[0].startswith(str(args.start_year)) or "-" in p[0] or int(p[1][:4]) >= args.start_year]

    TRADE_COL_MAP = {
        "signal_date": "信号日期",
        "rank": "排名",
        "rank_score": "评分",
        "code": "股票代码",
        "name": "股票名称",
        "entry_date": "买入日期",
        "entry_price": "买入价",
        "quantity": "买入股数",
        "buy_amount": "买入金额",
        "take_profit_price": "止盈价",
        "stop_loss_price": "止损价",
        "exit_date": "卖出日期",
        "exit_price": "卖出价",
        "sell_amount": "卖出金额",
        "holding_days": "持股天数",
        "exit_reason": "退出原因",
        "buy_fee": "买入费",
        "sell_fee": "卖出费",
        "total_fee": "总手续费",
        "pnl": "净收益金额",
        "return_pct": "收益率%",
        "cash_before_buy": "买前现金",
        "cash_after_exit": "卖后现金",
    }

    # 预加载所有 period 的原始日排名数据（所有 combo 共用）
    daily_cache: dict[str, pd.DataFrame] = {}
    for label, start_date, end_date in periods:
        if int(start_date[:4]) > args.end_year and label not in ("2021-至今连续",):
            continue
        daily = load_daily_rankings(args.daily_dir, start_date, end_date)
        if daily.empty:
            print(f"[{label}] 没有快照数据，跳过")
            continue
        daily_cache[label] = daily

    if not daily_cache:
        print("没有数据，请确认 daily_replay 目录存在 *_rebound_top3 快照")
        return

    # baseline（默认参数）用于写交易明细 sheet
    summary_rows: list[dict[str, object]] = []
    period_trades: dict[str, pd.DataFrame] = {}
    decision_log: list[dict[str, object]] = []
    for label, daily in daily_cache.items():
        candidates = select_top1(daily, args.min_latest_turn, args.max_current_drawdown,
                                 min_breadth=args.min_breadth)
        orders = build_orders(candidates, price_cache)
        trades, assets = simulate(orders, price_cache, args,
                                  max_gap_down=args.max_gap_down,
                                  min_gap_up=args.min_gap_up,
                                  min_rank_score=args.min_rank_score,
                                  decision_log=decision_log)
        row = summarize_period(label, trades, assets, args)
        summary_rows.append(row)
        if not trades.empty:
            period_trades[label] = trades.copy()

    summary_df = pd.DataFrame(summary_rows)

    # 合并全部交易（带周期列）
    all_frames: list[pd.DataFrame] = []
    for label, df in period_trades.items():
        df = df.copy()
        df.insert(0, "周期", label)
        all_frames.append(df)
    all_trades_df = pd.concat(all_frames, ignore_index=True) if all_frames else pd.DataFrame()

    # ── 表1：四种过滤条件组合对比 ──
    COMBO_LIST: list[tuple[str, float | None, float | None]] = [
        ("无过滤",          None, None),
        ("仅换手率>=7",     7.0,  None),
        ("仅回撤<=29",      None, 29.0),
        ("两者都用(默认)",  7.0,  29.0),
    ]
    combo_rows: list[dict[str, object]] = []
    for combo_label, min_turn, max_dd in COMBO_LIST:
        r = run_combo(daily_cache, price_cache, args, min_turn, max_dd)
        r["条件"] = combo_label
        combo_rows.append(r)
    combo_df = pd.DataFrame(combo_rows)
    cols_order = ["条件"] + [l for l in daily_cache] + ["交易数(不含连续)"]
    combo_df = combo_df[[c for c in cols_order if c in combo_df.columns]]

    # ── 表2：换手率阈值扫描（回撤固定 29）──
    turn_rows: list[dict[str, object]] = []
    for t in [5, 6, 7, 8, 9, 10]:
        r = run_combo(daily_cache, price_cache, args, float(t), 29.0)
        r["换手率阈值"] = f">={t}%"
        turn_rows.append(r)
    turn_df = pd.DataFrame(turn_rows)
    turn_cols = ["换手率阈值"] + [l for l in daily_cache] + ["交易数(不含连续)"]
    turn_df = turn_df[[c for c in turn_cols if c in turn_df.columns]]

    # ── 表3：回撤阈值扫描（换手率固定 7）──
    dd_rows: list[dict[str, object]] = []
    for d in [28, 29, 30, 31, 32, 33]:
        r = run_combo(daily_cache, price_cache, args, 7.0, float(d))
        r["回撤阈值"] = f"<={d}%"
        dd_rows.append(r)
    dd_df = pd.DataFrame(dd_rows)
    dd_cols = ["回撤阈值"] + [l for l in daily_cache] + ["交易数(不含连续)"]
    dd_df = dd_df[[c for c in dd_cols if c in dd_df.columns]]

    # 低开过滤对比（保留原有功能，基于默认条件的 orders）
    baseline_orders: dict[str, pd.DataFrame] = {}
    for label, daily in daily_cache.items():
        candidates = select_top1(daily, args.min_latest_turn, args.max_current_drawdown,
                                 min_breadth=args.min_breadth)
        orders = build_orders(candidates, price_cache)
        if not orders.empty:
            baseline_orders[label] = orders

    # (阈值描述, max_gap_down, min_gap_up)
    GAP_THRESHOLDS: list[tuple[str, float | None, float | None]] = [
        ("无过滤",                    None,  None),
        ("低开>1%",                  -1.0,  None),
        ("低开>2%",                  -2.0,  None),
        ("低开>3%",                  -3.0,  None),
        ("低开>5%",                  -5.0,  None),
        ("低开>2%+微高开跳过(0~3%)", -2.0,   3.0),
    ]
    gap_rows: list[dict[str, object]] = []
    for thresh_label, thresh_val, min_up in GAP_THRESHOLDS:
        row: dict[str, object] = {"阈值": thresh_label}
        total_trades = 0
        for label, orders in baseline_orders.items():
            t, a = simulate(orders, price_cache, args,
                            max_gap_down=thresh_val, min_gap_up=min_up)
            s = summarize_period(label, t, a, args)
            row[label] = f"{s['收益率_pct']:+.1f}% ({int(s['交易数'])}笔)"
            if label != "2021-至今连续":
                total_trades += int(s["交易数"])
        row["交易数(不含连续)"] = total_trades
        gap_rows.append(row)
    gap_df = pd.DataFrame(gap_rows)

    # ── 分数门槛扫描（固定默认参数 + 低开>2%，只变 min_rank_score）──
    SCORE_THRESHOLDS: list[float | None] = [None, 55, 60, 65, 70, 75, 80, 85, 90]
    score_rows: list[dict[str, object]] = []
    for score_thresh in SCORE_THRESHOLDS:
        row_s: dict[str, object] = {"分数门槛": "无过滤" if score_thresh is None else f">={score_thresh:.0f}"}
        total_trades_s = 0
        for label, orders in baseline_orders.items():
            t, a = simulate(orders, price_cache, args,
                            max_gap_down=-2.0, min_gap_up=args.min_gap_up,
                            min_rank_score=score_thresh)
            s = summarize_period(label, t, a, args)
            row_s[label] = f"{s['收益率_pct']:+.1f}% ({int(s['交易数'])}笔)"
            if label != "2021-至今连续":
                total_trades_s += int(s["交易数"])
        row_s["交易数(不含连续)"] = total_trades_s
        score_rows.append(row_s)
    score_df = pd.DataFrame(score_rows)

    # ── 信号广度对比（固定默认参数 + 低开>2%，只变 min_breadth）──
    # 广度改变选股集合，需对每个 breadth 重建 orders
    BREADTH_LEVELS = [1, 2, 3]
    breadth_rows: list[dict[str, object]] = []
    for bl in BREADTH_LEVELS:
        row_b: dict[str, object] = {"信号广度": "无过滤" if bl <= 1 else f">={bl}只"}
        total_trades_b = 0
        for label, daily in daily_cache.items():
            cand_b = select_top1(daily, args.min_latest_turn, args.max_current_drawdown, min_breadth=bl)
            orders_b = build_orders(cand_b, price_cache)
            t, a = simulate(orders_b, price_cache, args,
                            max_gap_down=args.max_gap_down, min_gap_up=args.min_gap_up)
            s = summarize_period(label, t, a, args)
            row_b[label] = f"{s['收益率_pct']:+.1f}% ({int(s['交易数'])}笔)"
            if label != "2021-至今连续":
                total_trades_b += int(s["交易数"])
        row_b["交易数(不含连续)"] = total_trades_b
        breadth_rows.append(row_b)
    breadth_df = pd.DataFrame(breadth_rows)

    # ── 含手续费 vs 免手续费 对比（基于 baseline_orders）──
    import copy as _copy
    args_nofee = _copy.copy(args)
    args_nofee.no_fees = True
    fee_rows: list[dict[str, object]] = []
    for fee_label, use_args in (("含手续费", args), ("免手续费", args_nofee)):
        row_f: dict[str, object] = {"口径": fee_label}
        for label, orders in baseline_orders.items():
            t, a = simulate(orders, price_cache, use_args,
                            max_gap_down=use_args.max_gap_down, min_gap_up=use_args.min_gap_up)
            s = summarize_period(label, t, a, use_args)
            row_f[label] = f"{s['收益率_pct']:+.1f}%"
        fee_rows.append(row_f)
    # 第三行：费用拖累（含费 - 免费）
    row_diff: dict[str, object] = {"口径": "费用拖累"}
    for label in baseline_orders:
        with_fee = float(str(fee_rows[0][label]).rstrip("%"))
        no_fee = float(str(fee_rows[1][label]).rstrip("%"))
        row_diff[label] = f"{with_fee - no_fee:+.1f}%"
    fee_rows.append(row_diff)
    fee_df = pd.DataFrame(fee_rows)

    # ── 网格搜索：换手率 × 回撤 全组合矩阵 ──
    GRID_TURNS = [3, 4, 5, 6, 7, 8, 9, 10]
    GRID_DDS   = [25, 26, 27, 28, 29, 30, 31, 32, 33, 35]
    CONT_LABEL = "2021-至今连续"
    grid_ret: dict[str, dict[str, str]] = {}   # {行标签: {列标签: 收益率字符串}}
    grid_cnt: dict[str, dict[str, str]] = {}
    _buf = io.StringIO()
    _orig_stdout = sys.stdout
    sys.stdout = _Tee(_orig_stdout, _buf)  # type: ignore[assignment]
    print("正在运行参数网格搜索，请稍候…")
    for t in GRID_TURNS:
        row_label = f">={t}%"
        grid_ret[row_label] = {}
        grid_cnt[row_label] = {}
        for d in GRID_DDS:
            col_label = f"<={d}%"
            r = run_combo(daily_cache, price_cache, args, float(t), float(d))
            cell_ret = r.get(CONT_LABEL, "N/A")
            # cell_ret 格式 "+773.2% (231笔)"，拆出收益率和笔数
            ret_str = cell_ret.split(" ")[0] if isinstance(cell_ret, str) else str(cell_ret)
            cnt_str = cell_ret.split("(")[1].rstrip(")") if isinstance(cell_ret, str) and "(" in cell_ret else "-"
            grid_ret[row_label][col_label] = ret_str
            grid_cnt[row_label][col_label] = cnt_str
    grid_ret_df = pd.DataFrame(grid_ret).T
    grid_ret_df.index.name = "换手率\\回撤"
    grid_cnt_df = pd.DataFrame(grid_cnt).T
    grid_cnt_df.index.name = "换手率\\回撤"

    print("\n" + "=" * 70)
    print(f"rebound_top3 策略年度收益率（换手率>={args.min_latest_turn}%, 回撤<={args.max_current_drawdown}%, top1）")
    print("=" * 70)
    print(f"初始本金: {args.initial_capital:,.0f}  止盈: {args.take_profit*100:.0f}%  止损: {args.stop_loss*100:.0f}%  最长持仓: {args.max_hold_days}天")
    print(f"持仓规则: 同时最多1只 | 卖出当日不可买，次日起才可买入 | 信号广度>={args.min_breadth}只(市场共振才交易)")
    if getattr(args, "no_fees", False):
        print("手续费: 不扣除（--no-fees）")
    else:
        print(f"手续费: 佣金{args.commission_rate*10000:.1f}‱(双边,最低{args.min_commission:.0f}元) + "
              f"印花税{args.stamp_duty_rate_old*100:.2f}%/{args.stamp_duty_rate*100:.2f}%(卖出,{args.stamp_duty_cut_date}前后) + "
              f"过户费{args.transfer_fee_rate*100:.3f}%(双边)")
    print()

    display = summary_df[["周期", "交易数", "胜率_pct", "总收益", "收益率_pct", "最大回撤_pct", "总手续费"]].copy()
    display.columns = ["周期", "交易数", "胜率%", "总收益(含费)", "收益率%", "最大回撤%", "总手续费"]
    print(display.to_string(index=False))
    print()
    print("── 含手续费 vs 免手续费 对比（基于默认条件）──")
    print(fee_df.to_string(index=False))
    print()
    print("── 表1：四种过滤条件组合对比 ──")
    print(combo_df.to_string(index=False))
    print()
    print("── 表2：换手率阈值扫描（回撤固定<=29%）──")
    print(turn_df.to_string(index=False))
    print()
    print("── 表3：回撤阈值扫描（换手率固定>=7%）──")
    print(dd_df.to_string(index=False))
    print()
    print("── 低开过滤对比（基于默认条件）──")
    print(gap_df.to_string(index=False))
    print()
    print("── rank_score 分数门槛扫描（固定：换手率>=5%, 回撤<=33%, 低开>2%）──")
    print(score_df.to_string(index=False))
    print()
    print("── 信号广度对比（当日候选数 >= N 才交易；其余=默认参数+低开>2%）──")
    print(breadth_df.to_string(index=False))
    print()
    print("── 参数网格搜索：换手率×回撤 → 2021-至今连续收益率 ──")
    print(grid_ret_df.to_string())
    print()
    print("── 参数网格搜索：换手率×回撤 → 2021-至今连续交易笔数 ──")
    print(grid_cnt_df.to_string())
    print()

    # ── 关键组合逐年收益明细 ──
    FOCUS_TURNS = [4, 5, 6]
    FOCUS_DDS   = [28, 29, 30, 31, 32, 33]
    focus_rows: list[dict[str, object]] = []
    for t in FOCUS_TURNS:
        for d in FOCUS_DDS:
            r = run_combo(daily_cache, price_cache, args, float(t), float(d))
            r["组合"] = f"换手率>={t}%, 回撤<={d}%"
            focus_rows.append(r)
    focus_df = pd.DataFrame(focus_rows)
    focus_cols = ["组合"] + [lbl for lbl in daily_cache] + ["交易数(不含连续)"]
    focus_df = focus_df[[c for c in focus_cols if c in focus_df.columns]]
    # 叠加低开>2%+微高开过滤的第二套结果（新规则）
    focus_gap_rows: list[dict[str, object]] = []
    for t in FOCUS_TURNS:
        for d in FOCUS_DDS:
            r = run_combo(daily_cache, price_cache, args, float(t), float(d),
                          max_gap_down=-2.0, min_gap_up=args.min_gap_up)
            r["组合"] = f"换手率>={t}%, 回撤<={d}%"
            focus_gap_rows.append(r)
    focus_gap_df = pd.DataFrame(focus_gap_rows)
    focus_gap_df = focus_gap_df[[c for c in focus_cols if c in focus_gap_df.columns]]

    def _to_box(df: pd.DataFrame, title: str) -> None:
        _year_cols = ["2021", "2022", "2023", "2024", "2025", "2026年1-5月"]
        _cols = ["组合"] + [c for c in _year_cols if c in df.columns] + ["2021-至今连续"]
        _df = df[[c for c in _cols if c in df.columns]].rename(columns={"2021-至今连续": "累计"})
        for col in _df.columns:
            if col != "组合":
                _df[col] = _df[col].apply(lambda v: str(v).split(" ")[0] if pd.notna(v) else str(v))
        print(title)
        print(format_box_table(list(_df.columns), [list(row) for _, row in _df.iterrows()]))
        print()

    _to_box(focus_df,     "── 关键组合逐年收益（无低开过滤）──")
    _to_box(focus_gap_df, "── 关键组合逐年收益（叠加低开>2%）──")

    # ── 持仓天数对比 ──
    HOLD_DAYS = [5, 10, 15]
    hold_rows: list[dict[str, object]] = []
    for hd in HOLD_DAYS:
        args_copy = copy.copy(args)
        args_copy.max_hold_days = hd
        r = run_combo(daily_cache, price_cache, args_copy,
                      args.min_latest_turn, args.max_current_drawdown,
                      max_gap_down=args.max_gap_down, min_gap_up=args.min_gap_up)
        r["最长持仓天数"] = hd
        hold_rows.append(r)
    hold_df = pd.DataFrame(hold_rows)
    hold_cols = ["最长持仓天数"] + [lbl for lbl in daily_cache if lbl != "2021-至今连续"] + ["2021-至今连续"]
    hold_df_out = hold_df[[c for c in hold_cols if c in hold_df.columns]]

    def _to_box_hold(df: pd.DataFrame) -> None:
        _year_cols = ["2021", "2022", "2023", "2024", "2025", "2026年1-5月"]
        _cols = ["最长持仓天数"] + [c for c in _year_cols if c in df.columns] + ["2021-至今连续"]
        _df = df[[c for c in _cols if c in df.columns]].rename(columns={"2021-至今连续": "累计"})
        for col in _df.columns:
            if col != "最长持仓天数":
                _df[col] = _df[col].apply(lambda v: str(v).split(" ")[0] if pd.notna(v) else str(v))
        _df["最长持仓天数"] = _df["最长持仓天数"].astype(str) + "天"
        print(f"── 持仓天数对比（换手率>=5%, 回撤<=33%, 低开>2%+微高开跳过0~3%）──")
        print(format_box_table(list(_df.columns), [list(row) for _, row in _df.iterrows()]))
        print()

    _to_box_hold(hold_df_out)

    def to_cn(df: pd.DataFrame) -> pd.DataFrame:
        return df.rename(columns={k: v for k, v in TRADE_COL_MAP.items() if k in df.columns})

    args.output.parent.mkdir(parents=True, exist_ok=True)
    with pd.ExcelWriter(args.output) as writer:
        summary_df.to_excel(writer, index=False, sheet_name="年度汇总")
        combo_df.to_excel(writer, index=False, sheet_name="条件组合对比")
        # 换手率扫描 + 回撤扫描写同一 sheet，中间空行
        ws_name = "阈值扫描"
        turn_df.to_excel(writer, index=False, sheet_name=ws_name, startrow=0)
        dd_df.to_excel(writer, index=False, sheet_name=ws_name, startrow=len(turn_df) + 3)
        gap_df.to_excel(writer, index=False, sheet_name="低开过滤对比")
        score_df.to_excel(writer, index=False, sheet_name="分数门槛扫描")
        breadth_df.to_excel(writer, index=False, sheet_name="信号广度对比")
        fee_df.to_excel(writer, index=False, sheet_name="含费vs免费对比")
        grid_ret_df.to_excel(writer, sheet_name="参数网格搜索", startrow=0)
        grid_cnt_df.to_excel(writer, sheet_name="参数网格搜索", startrow=len(grid_ret_df) + 3)
        focus_df.to_excel(writer, index=False, sheet_name="关键组合逐年明细")
        focus_gap_df.to_excel(writer, index=False, sheet_name="关键组合+低开过滤")
        hold_df_out.to_excel(writer, index=False, sheet_name="持仓天数对比")

        # 按年分 sheet
        for label, df in period_trades.items():
            sheet = f"{label}_交易明细"[:31]
            to_cn(df).to_excel(writer, index=False, sheet_name=sheet)

        # 全部交易合并 sheet
        if not all_trades_df.empty:
            cn_all = all_trades_df.rename(columns={k: v for k, v in TRADE_COL_MAP.items() if k in all_trades_df.columns})
            cn_all.to_excel(writer, index=False, sheet_name="全部交易明细")

        min_gap_desc = (f"开盘 0%~{args.min_gap_up}% 之间跳过，大高开>={args.min_gap_up}% 才买"
                        if args.min_gap_up is not None else "不过滤")
        pd.DataFrame([
            ("数据来源", "daily_replay/*_rebound_top3/ranking_snapshot.csv"),
            ("选股条件", f"latest_turn >= {args.min_latest_turn}, current_drawdown_pct <= {args.max_current_drawdown}, 取 rank 第 1"),
            ("信号广度", f"当日过滤后候选数 >= {args.min_breadth} 只才交易（市场共振确认）"),
            ("买入", "信号日后第 1 个交易日开盘价"),
            ("初始本金", args.initial_capital),
            ("仓位管理", "全仓（可用现金买满整手，100 股/手）"),
            ("止盈", f"{args.take_profit*100:.0f}%（买入价 × {1 + args.take_profit:.2f}）"),
            ("止损", f"{args.stop_loss*100:.0f}%（买入价 × {1 - args.stop_loss:.2f}）"),
            ("最长持仓", f"{args.max_hold_days} 个交易日，到期按收盘价卖出"),
            ("低开过滤", f"低开>{abs(args.max_gap_down):.0f}% 跳过（开盘<昨收×{1+args.max_gap_down:.2f})"),
            ("微高开过滤", min_gap_desc),
            ("持仓规则", "同时最多1只；卖出当日不可买，次日起可买入"),
            ("同股冷却", f"卖出后 {args.reentry_cooldown_days} 个交易日内不重复买入"),
            ("退出优先级", "止损 > 止盈 > 到期（同日先判断止损）"),
            ("手续费-佣金", f"{args.commission_rate*10000:.1f}‱ 双边各收，单笔最低 {args.min_commission:.0f} 元" if not args.no_fees else "不扣除"),
            ("手续费-印花税", f"卖出单边：{args.stamp_duty_cut_date} 前 {args.stamp_duty_rate_old*100:.2f}%，之后 {args.stamp_duty_rate*100:.2f}%" if not args.no_fees else "不扣除"),
            ("手续费-过户费", f"{args.transfer_fee_rate*100:.3f}% 双边各收" if not args.no_fees else "不扣除"),
        ], columns=["项目", "说明"]).to_excel(writer, index=False, sheet_name="参数说明")

    # 决策日志另存 CSV
    if decision_log:
        decision_log_path = args.output.with_name(args.output.stem + "_decision_log.csv")
        pd.DataFrame(decision_log).to_csv(decision_log_path, index=False, encoding="utf-8-sig")
        print(f"已保存(决策日志): {decision_log_path.resolve()}")

    # 全部交易明细另存 CSV
    if not all_trades_df.empty:
        cn_all_csv = all_trades_df.rename(columns={k: v for k, v in TRADE_COL_MAP.items() if k in all_trades_df.columns})
        trades_csv_path = args.output.with_name(args.output.stem + "_trades.csv")
        cn_all_csv.to_csv(trades_csv_path, index=False, encoding="utf-8-sig")
        print(f"已保存(交易明细): {trades_csv_path.resolve()}")

    sys.stdout = _orig_stdout
    txt_path = args.output.with_suffix(".txt")
    txt_path.write_text(_buf.getvalue(), encoding="utf-8")
    print(f"已保存(分析报告): {args.output.resolve()}")
    print(f"已保存(文本报告): {txt_path.resolve()}")


if __name__ == "__main__":
    main()
