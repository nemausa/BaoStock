from __future__ import annotations

import argparse
from datetime import date
from pathlib import Path

import pandas as pd


DAILY_REPLAY_DIR = Path("a_stock_data/verification_records/daily_replay")
PARQUET_DIR = Path("a_stock_data/parquet")
OUTPUT_DIR = Path("strategy_recommended_runner/outputs")

DEFAULT_PERIODS = [
    ("2021", "2021-01-01", "2021-12-31"),
    ("2022", "2022-01-01", "2022-12-31"),
    ("2023", "2023-01-01", "2023-12-31"),
    ("2024", "2024-01-01", "2024-12-31"),
    ("2025", "2025-01-01", "2025-12-31"),
    ("2026-至今", "2026-01-01", date.today().strftime("%Y-%m-%d")),
    ("2021-至今连续", "2021-01-01", date.today().strftime("%Y-%m-%d")),
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="统计 rebound_top3 策略每年收益率。")
    parser.add_argument("--start-year", type=int, default=2021)
    parser.add_argument("--end-year", type=int, default=date.today().year)
    parser.add_argument("--daily-dir", type=Path, default=DAILY_REPLAY_DIR)
    parser.add_argument("--parquet-dir", type=Path, default=PARQUET_DIR)
    parser.add_argument("--min-latest-turn", type=float, default=8.0)
    parser.add_argument("--max-current-drawdown", type=float, default=29.0)
    parser.add_argument("--take-profit", type=float, default=0.08)
    parser.add_argument("--stop-loss", type=float, default=0.03)
    parser.add_argument("--max-hold-days", type=int, default=15)
    parser.add_argument("--reentry-cooldown-days", type=int, default=10)
    parser.add_argument("--initial-capital", type=float, default=50000.0)
    parser.add_argument("--lot-size", type=int, default=100)
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
            df = pd.read_parquet(path, columns=["date", "open", "high", "low", "close"])
            df = df.copy()
            df["date"] = pd.to_datetime(df["date"], errors="coerce")
            for col in ["open", "high", "low", "close"]:
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
    return {
        "entry_index": int(future.index[0]),
        "entry_date": str(row["date_str"]),
        "entry_open": float(row["open"]),
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

    for idx in range(entry_index, last_index + 1):
        row = df.loc[idx]
        holding_days = idx - entry_index + 1
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


def select_top1(daily: pd.DataFrame, args: argparse.Namespace) -> pd.DataFrame:
    if daily.empty:
        return daily
    filtered = daily[
        (numeric(daily, "latest_turn") >= args.min_latest_turn)
        & (numeric(daily, "current_drawdown_pct") <= args.max_current_drawdown)
    ].copy()
    if filtered.empty:
        return filtered
    return filtered.sort_values(["signal_date", "rank"]).groupby("signal_date", group_keys=False).head(1).reset_index(drop=True)


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


def simulate(orders: pd.DataFrame, price_cache: PriceCache, args: argparse.Namespace) -> tuple[pd.DataFrame, pd.DataFrame]:
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

        if entry_date <= busy_until:
            continue
        if code in cooldown and entry_date <= cooldown[code]:
            continue

        entry_price = float(order["entry_open"])
        lot_cost = entry_price * int(args.lot_size)
        quantity = int(cash // lot_cost) * int(args.lot_size) if lot_cost > 0 else 0
        if quantity < int(args.lot_size):
            continue

        buy_amount = quantity * entry_price
        cash_before_buy = cash
        cash_after_entry = cash - buy_amount
        take_profit_price = entry_price * (1 + args.take_profit)
        stop_loss_price = entry_price * (1 - args.stop_loss)

        exit_info = exit_trade(price_cache, code, int(order["entry_index"]), entry_price, args)
        exit_price = float(exit_info["exit_price"])
        sell_amount = quantity * exit_price
        pnl = sell_amount - buy_amount
        cash = cash_after_entry + sell_amount

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
            "pnl": round(pnl, 2),
            "return_pct": round(pnl / buy_amount * 100 if buy_amount else 0.0, 2),
            "cash_before_buy": round(cash_before_buy, 2),
            "cash_after_exit": round(cash, 2),
        })

        busy_until = str(exit_info["exit_date"])
        cooldown[code] = cooldown_until(price_cache, code, str(exit_info["exit_date"]), int(args.reentry_cooldown_days))

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

    take_profit = int((trades["exit_reason"] == "止盈").sum()) if not trades.empty else 0
    stop_loss = int((trades["exit_reason"] == "止损").sum()) if not trades.empty else 0
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
        "pnl": "收益金额",
        "return_pct": "收益率%",
        "cash_before_buy": "买前现金",
        "cash_after_exit": "卖后现金",
    }

    summary_rows: list[dict[str, object]] = []
    period_trades: dict[str, pd.DataFrame] = {}

    for label, start_date, end_date in periods:
        if int(start_date[:4]) > args.end_year and label not in ("2021-至今连续",):
            continue

        daily = load_daily_rankings(args.daily_dir, start_date, end_date)
        if daily.empty:
            print(f"[{label}] 没有快照数据，跳过")
            continue

        candidates = select_top1(daily, args)
        orders = build_orders(candidates, price_cache)
        trades, assets = simulate(orders, price_cache, args)

        row = summarize_period(label, trades, assets, args)
        summary_rows.append(row)

        if not trades.empty:
            period_trades[label] = trades.copy()

    if not summary_rows:
        print("没有数据，请确认 daily_replay 目录存在 *_rebound_top3 快照")
        return

    summary_df = pd.DataFrame(summary_rows)

    # 合并全部交易（带周期列）
    all_frames: list[pd.DataFrame] = []
    for label, df in period_trades.items():
        df = df.copy()
        df.insert(0, "周期", label)
        all_frames.append(df)
    all_trades_df = pd.concat(all_frames, ignore_index=True) if all_frames else pd.DataFrame()

    print("\n" + "=" * 70)
    print("rebound_top3 策略年度收益率（latest_turn>=8, current_drawdown_pct<=29, top1）")
    print("=" * 70)
    print(f"初始本金: {args.initial_capital:,.0f}  止盈: {args.take_profit*100:.0f}%  止损: {args.stop_loss*100:.0f}%  最长持仓: {args.max_hold_days}天")
    print()

    display = summary_df[["周期", "交易数", "胜率_pct", "总收益", "收益率_pct", "最大回撤_pct"]].copy()
    display.columns = ["周期", "交易数", "胜率%", "总收益", "收益率%", "最大回撤%"]
    print(display.to_string(index=False))
    print()

    def to_cn(df: pd.DataFrame) -> pd.DataFrame:
        return df.rename(columns={k: v for k, v in TRADE_COL_MAP.items() if k in df.columns})

    args.output.parent.mkdir(parents=True, exist_ok=True)
    with pd.ExcelWriter(args.output) as writer:
        summary_df.to_excel(writer, index=False, sheet_name="年度汇总")

        # 按年分 sheet
        for label, df in period_trades.items():
            sheet = f"{label}_交易明细"[:31]
            to_cn(df).to_excel(writer, index=False, sheet_name=sheet)

        # 全部交易合并 sheet
        if not all_trades_df.empty:
            cn_all = all_trades_df.rename(columns={k: v for k, v in TRADE_COL_MAP.items() if k in all_trades_df.columns})
            cn_all.to_excel(writer, index=False, sheet_name="全部交易明细")

        pd.DataFrame([
            ("数据来源", "daily_replay/*_rebound_top3/ranking_snapshot.csv"),
            ("选股条件", f"latest_turn >= {args.min_latest_turn}, current_drawdown_pct <= {args.max_current_drawdown}, 取 rank 第 1"),
            ("买入", "信号日后第 1 个交易日开盘价"),
            ("初始本金", args.initial_capital),
            ("仓位管理", "全仓（可用现金买满整手，100 股/手）"),
            ("止盈", f"{args.take_profit*100:.0f}%（买入价 × {1 + args.take_profit:.2f}）"),
            ("止损", f"{args.stop_loss*100:.0f}%（买入价 × {1 - args.stop_loss:.2f}）"),
            ("最长持仓", f"{args.max_hold_days} 个交易日，到期按收盘价卖出"),
            ("同股冷却", f"卖出后 {args.reentry_cooldown_days} 个交易日内不重复买入"),
            ("退出优先级", "止损 > 止盈 > 到期（同日先判断止损）"),
            ("手续费", "不扣除"),
        ], columns=["项目", "说明"]).to_excel(writer, index=False, sheet_name="参数说明")

    print(f"已保存: {args.output.resolve()}")


if __name__ == "__main__":
    main()
