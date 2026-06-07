from __future__ import annotations

import argparse
import random
import time
from pathlib import Path

import pandas as pd
from tqdm import tqdm

import update_a_stock_baostock as updater
import verification_records as vr


DEFAULT_START_DATE = "2021-01-01"
DEFAULT_END_DATE = "2024-12-31"
DEFAULT_YEARS = [2021, 2022, 2023, 2024]

COMMISSION_RATE = 0.00025
STAMP_DUTY_RATE = 0.0005
TRANSFER_FEE_RATE = 0.0001


def append_log(path: Path, row: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    frame = pd.DataFrame([row])
    frame.to_csv(
        path,
        mode="a",
        index=False,
        header=not path.exists(),
        encoding="utf-8-sig",
    )


def has_historical_window(df: pd.DataFrame, start_date: str, end_date: str) -> bool:
    if df.empty or "date" not in df.columns:
        return False

    dates = pd.to_datetime(df["date"], errors="coerce").dropna()
    if dates.empty:
        return False

    window = dates[(dates >= pd.Timestamp(start_date)) & (dates <= pd.Timestamp(end_date))]
    return not window.empty and window.max() >= pd.Timestamp(end_date)


def download_history(args: argparse.Namespace) -> None:
    updater.ensure_dirs()
    shard = f"offset{args.offset}_limit{args.limit or 'all'}"
    log_path = Path(f"a_stock_data/logs/historical_backfill_2021_2024_{shard}.csv")

    updater.login_baostock()
    try:
        stock_list = updater.get_stock_list()
        if args.offset:
            stock_list = stock_list.iloc[args.offset:]
        if args.limit:
            stock_list = stock_list.head(args.limit)

        updated_count = 0
        skipped_count = 0
        empty_count = 0
        failed_count = 0
        updated_rows = 0

        for _, stock in tqdm(stock_list.iterrows(), total=len(stock_list)):
            code = updater.normalize_code(stock["code"])
            bs_code = str(stock["bs_code"])
            name = str(stock["name"])

            try:
                old_df = updater.read_stock_data(code)
                if not args.force and has_historical_window(old_df, args.start_date, args.end_date):
                    skipped_count += 1
                    continue

                new_df = pd.DataFrame()
                last_error: Exception | None = None
                for attempt in range(args.max_retry):
                    try:
                        with updater.query_timeout(args.query_timeout):
                            new_df = updater.fetch_daily_history(bs_code, args.start_date, args.end_date)
                        last_error = None
                        break
                    except Exception as exc:
                        last_error = exc
                        time.sleep(1.0 * (attempt + 1))

                if last_error is not None:
                    failed_count += 1
                    append_log(log_path, {
                        "code": code,
                        "name": name,
                        "start_date": args.start_date,
                        "end_date": args.end_date,
                        "status": "failed",
                        "rows": 0,
                        "reason": f"{type(last_error).__name__}: {last_error}",
                    })
                    continue

                if new_df.empty:
                    empty_count += 1
                    append_log(log_path, {
                        "code": code,
                        "name": name,
                        "start_date": args.start_date,
                        "end_date": args.end_date,
                        "status": "empty",
                        "rows": 0,
                        "reason": "",
                    })
                    continue

                merged = updater.merge_stock_data(old_df, new_df)
                updater.write_stock_data(code, merged)
                updated_count += 1
                updated_rows += len(new_df)
                append_log(log_path, {
                    "code": code,
                    "name": name,
                    "start_date": args.start_date,
                    "end_date": args.end_date,
                    "status": "updated",
                    "rows": len(new_df),
                    "reason": "",
                })
            except Exception as exc:
                failed_count += 1
                append_log(log_path, {
                    "code": code,
                    "name": name,
                    "start_date": args.start_date,
                    "end_date": args.end_date,
                    "status": "failed",
                    "rows": 0,
                    "reason": f"{type(exc).__name__}: {exc}",
                })

            time.sleep(random.uniform(args.sleep_min, args.sleep_max))

        print("历史行情补齐完成")
        print(f"更新股票数: {updated_count}")
        print(f"新增记录数: {updated_rows}")
        print(f"跳过股票数: {skipped_count}")
        print(f"空数据股票数: {empty_count}")
        print(f"失败股票数: {failed_count}")
        print(f"日志: {log_path.resolve()}")
    finally:
        updater.logout_baostock()


def backfill_daily(args: argparse.Namespace) -> None:
    for year in args.years:
        start = f"{year}-01-01"
        end = f"{year}-12-31"
        print(f"生成每日回放: {start} 到 {end}")
        replay_args = argparse.Namespace(
            start_date=start,
            end_date=end,
            top_n=3,
            record_dir=vr.RECORD_DIR,
            suffix=args.suffix,
            trend_threshold=args.trend_threshold,
            drawdown_min=args.drawdown_min,
            drawdown_max=args.drawdown_max,
            bottom_area_max=args.bottom_area_max,
            current_rebound_max=args.current_rebound_max,
            full_rebound_min=args.full_rebound_min,
            min_events=args.min_events,
            include_st=args.include_st,
            progress=args.progress,
            verbose_errors=args.verbose_errors,
        )
        vr.backfill_daily_snapshots(replay_args)


def buy_fee(amount: float) -> float:
    return amount * (COMMISSION_RATE + TRANSFER_FEE_RATE)


def sell_fee(amount: float) -> float:
    return amount * (COMMISSION_RATE + TRANSFER_FEE_RATE + STAMP_DUTY_RATE)


def max_quantity(cash: float, price: float, lot_size: int) -> int:
    lot_cost = price * lot_size * (1 + COMMISSION_RATE + TRANSFER_FEE_RATE)
    lots = int(cash // lot_cost) if lot_cost > 0 else 0
    return lots * lot_size


def max_quantity_no_fees(cash: float, price: float, lot_size: int) -> int:
    lot_cost = price * lot_size
    lots = int(cash // lot_cost) if lot_cost > 0 else 0
    return lots * lot_size


def simulate_open_buy_with_fees(
    orders: pd.DataFrame,
    price_cache: dict[str, pd.DataFrame],
    args: argparse.Namespace,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    if orders.empty:
        return pd.DataFrame(), pd.DataFrame(), summary_frame({}, args)

    lookup = vr.price_lookup_for_codes(price_cache)
    dates = vr.portfolio_trading_dates(price_cache, orders, args)
    orders_by_entry = {date: group.copy() for date, group in orders.groupby("entry_date")}

    cash = float(args.initial_capital)
    holdings: list[dict[str, object]] = []
    trades: list[dict[str, object]] = []
    assets: list[dict[str, object]] = []
    cooldown_until: dict[str, pd.Timestamp] = {}
    skipped_count = 0

    for trade_date in dates:
        trade_date_str = trade_date.strftime("%Y-%m-%d")
        todays_orders = orders_by_entry.get(trade_date_str, pd.DataFrame())
        daily_buy_count = 0

        for _, order in todays_orders.iterrows():
            code = vr.normalize_code(order["code"])
            if daily_buy_count >= args.max_daily_buys:
                skipped_count += 1
                continue
            if len(holdings) >= args.max_positions:
                skipped_count += 1
                continue
            if any(str(holding["code"]) == code for holding in holdings):
                skipped_count += 1
                continue
            if code in cooldown_until and trade_date <= cooldown_until[code]:
                skipped_count += 1
                continue

            entry_price = float(order["entry_open"])
            quantity = max_quantity(cash, entry_price, args.lot_size)
            if quantity < args.lot_size:
                skipped_count += 1
                continue

            buy_amount = entry_price * quantity
            fee = buy_fee(buy_amount)
            cash -= buy_amount + fee
            holdings.append({
                "code": code,
                "name": order.get("name", ""),
                "signal": order,
                "signal_date": pd.Timestamp(order["signal_date"]),
                "entry_date": trade_date,
                "entry_price": entry_price,
                "quantity": quantity,
                "buy_amount": buy_amount,
                "buy_fee": fee,
                "take_profit_price": entry_price * (1 + args.take_profit),
                "stop_loss_price": entry_price * (1 - args.stop_loss),
                "holding_days": 0,
            })
            daily_buy_count += 1

        remaining: list[dict[str, object]] = []
        for holding in holdings:
            code = str(holding["code"])
            row = lookup.get(code, {}).get(trade_date)
            if row is None:
                remaining.append(holding)
                continue

            holding["holding_days"] = int(holding["holding_days"]) + 1
            high = float(row["high"])
            low = float(row["low"])
            close = float(row["close"])
            exit_price: float | None = None
            exit_reason = ""

            if low <= float(holding["stop_loss_price"]):
                exit_price = float(holding["stop_loss_price"])
                exit_reason = "止损"
            elif high >= float(holding["take_profit_price"]):
                exit_price = float(holding["take_profit_price"])
                exit_reason = "止盈"
            elif int(holding["holding_days"]) >= args.max_hold_days:
                exit_price = close
                exit_reason = "到期卖出"

            if exit_price is None:
                remaining.append(holding)
                continue

            sell_amount = exit_price * int(holding["quantity"])
            fee = sell_fee(sell_amount)
            cash += sell_amount - fee
            gross_pnl = sell_amount - float(holding["buy_amount"])
            total_fee = float(holding["buy_fee"]) + fee
            net_pnl = gross_pnl - total_fee
            trades.append({
                **holding["signal"].to_dict(),
                "code": code,
                "name": holding.get("name", ""),
                "signal_date": pd.Timestamp(holding["signal_date"]).strftime("%Y-%m-%d"),
                "entry_date": pd.Timestamp(holding["entry_date"]).strftime("%Y-%m-%d"),
                "entry_price": float(holding["entry_price"]),
                "quantity": int(holding["quantity"]),
                "buy_amount": float(holding["buy_amount"]),
                "buy_fee": float(holding["buy_fee"]),
                "exit_date": trade_date.strftime("%Y-%m-%d"),
                "exit_price": exit_price,
                "sell_amount": sell_amount,
                "sell_fee_tax": fee,
                "exit_reason": exit_reason,
                "holding_days": int(holding["holding_days"]),
                "gross_pnl": gross_pnl,
                "total_fee_tax": total_fee,
                "net_pnl": net_pnl,
                "net_return_pct": net_pnl / (float(holding["buy_amount"]) + float(holding["buy_fee"])) * 100,
                "cash_after_exit": cash,
            })
            cooldown_until[code] = vr.cooldown_until_date(price_cache[code], trade_date, args.reentry_cooldown_days)

        holdings = remaining
        market_value = 0.0
        holding_parts: list[str] = []
        for holding in holdings:
            code = str(holding["code"])
            row = lookup.get(code, {}).get(trade_date)
            mark_price = float(row["close"]) if row is not None else float(holding["entry_price"])
            market_value += mark_price * int(holding["quantity"])
            holding_parts.append(f"{code}:{int(holding['quantity'])}")

        assets.append({
            "date": trade_date_str,
            "cash": cash,
            "market_value": market_value,
            "total_asset": cash + market_value,
            "holdings": ",".join(holding_parts),
        })

    trades_df = pd.DataFrame(trades)
    assets_df = pd.DataFrame(assets)
    summary = summary_frame({
        "trades": trades_df,
        "assets": assets_df,
        "skipped_count": skipped_count,
    }, args)
    return trades_df, assets_df, summary


def simulate_open_buy_no_fees(
    orders: pd.DataFrame,
    price_cache: dict[str, pd.DataFrame],
    args: argparse.Namespace,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    if orders.empty:
        return pd.DataFrame(), pd.DataFrame(), summary_frame({}, args)

    lookup = vr.price_lookup_for_codes(price_cache)
    dates = vr.portfolio_trading_dates(price_cache, orders, args)
    orders_by_entry = {date: group.copy() for date, group in orders.groupby("entry_date")}

    cash = float(args.initial_capital)
    holdings: list[dict[str, object]] = []
    trades: list[dict[str, object]] = []
    assets: list[dict[str, object]] = []
    cooldown_until: dict[str, pd.Timestamp] = {}
    skipped_count = 0

    for trade_date in dates:
        trade_date_str = trade_date.strftime("%Y-%m-%d")
        todays_orders = orders_by_entry.get(trade_date_str, pd.DataFrame())
        daily_buy_count = 0

        for _, order in todays_orders.iterrows():
            code = vr.normalize_code(order["code"])
            if daily_buy_count >= args.max_daily_buys:
                skipped_count += 1
                continue
            if len(holdings) >= args.max_positions:
                skipped_count += 1
                continue
            if any(str(holding["code"]) == code for holding in holdings):
                skipped_count += 1
                continue
            if code in cooldown_until and trade_date <= cooldown_until[code]:
                skipped_count += 1
                continue

            entry_price = float(order["entry_open"])
            quantity = max_quantity_no_fees(cash, entry_price, args.lot_size)
            if quantity < args.lot_size:
                skipped_count += 1
                continue

            buy_amount = entry_price * quantity
            cash -= buy_amount
            holdings.append({
                "code": code,
                "name": order.get("name", ""),
                "signal": order,
                "signal_date": pd.Timestamp(order["signal_date"]),
                "entry_date": trade_date,
                "entry_price": entry_price,
                "quantity": quantity,
                "buy_amount": buy_amount,
                "buy_fee": 0.0,
                "take_profit_price": entry_price * (1 + args.take_profit),
                "stop_loss_price": entry_price * (1 - args.stop_loss),
                "holding_days": 0,
            })
            daily_buy_count += 1

        remaining: list[dict[str, object]] = []
        for holding in holdings:
            code = str(holding["code"])
            row = lookup.get(code, {}).get(trade_date)
            if row is None:
                remaining.append(holding)
                continue

            holding["holding_days"] = int(holding["holding_days"]) + 1
            high = float(row["high"])
            low = float(row["low"])
            close = float(row["close"])
            exit_price: float | None = None
            exit_reason = ""

            if low <= float(holding["stop_loss_price"]):
                exit_price = float(holding["stop_loss_price"])
                exit_reason = "止损"
            elif high >= float(holding["take_profit_price"]):
                exit_price = float(holding["take_profit_price"])
                exit_reason = "止盈"
            elif int(holding["holding_days"]) >= args.max_hold_days:
                exit_price = close
                exit_reason = "到期卖出"

            if exit_price is None:
                remaining.append(holding)
                continue

            sell_amount = exit_price * int(holding["quantity"])
            cash += sell_amount
            gross_pnl = sell_amount - float(holding["buy_amount"])
            trades.append({
                **holding["signal"].to_dict(),
                "code": code,
                "name": holding.get("name", ""),
                "signal_date": pd.Timestamp(holding["signal_date"]).strftime("%Y-%m-%d"),
                "entry_date": pd.Timestamp(holding["entry_date"]).strftime("%Y-%m-%d"),
                "entry_price": float(holding["entry_price"]),
                "quantity": int(holding["quantity"]),
                "buy_amount": float(holding["buy_amount"]),
                "buy_fee": 0.0,
                "exit_date": trade_date.strftime("%Y-%m-%d"),
                "exit_price": exit_price,
                "sell_amount": sell_amount,
                "sell_fee_tax": 0.0,
                "exit_reason": exit_reason,
                "holding_days": int(holding["holding_days"]),
                "gross_pnl": gross_pnl,
                "total_fee_tax": 0.0,
                "net_pnl": gross_pnl,
                "net_return_pct": gross_pnl / float(holding["buy_amount"]) * 100,
                "cash_after_exit": cash,
            })
            cooldown_until[code] = vr.cooldown_until_date(price_cache[code], trade_date, args.reentry_cooldown_days)

        holdings = remaining
        market_value = 0.0
        holding_parts: list[str] = []
        for holding in holdings:
            code = str(holding["code"])
            row = lookup.get(code, {}).get(trade_date)
            mark_price = float(row["close"]) if row is not None else float(holding["entry_price"])
            market_value += mark_price * int(holding["quantity"])
            holding_parts.append(f"{code}:{int(holding['quantity'])}")

        assets.append({
            "date": trade_date_str,
            "cash": cash,
            "market_value": market_value,
            "total_asset": cash + market_value,
            "holdings": ",".join(holding_parts),
        })

    trades_df = pd.DataFrame(trades)
    assets_df = pd.DataFrame(assets)
    summary = summary_frame({
        "trades": trades_df,
        "assets": assets_df,
        "skipped_count": skipped_count,
    }, args)
    return trades_df, assets_df, summary


def summary_frame(data: dict[str, object], args: argparse.Namespace) -> pd.DataFrame:
    trades = data.get("trades")
    assets = data.get("assets")
    skipped_count = int(data.get("skipped_count", 0))
    trades_df = trades if isinstance(trades, pd.DataFrame) else pd.DataFrame()
    assets_df = assets if isinstance(assets, pd.DataFrame) else pd.DataFrame()

    final_asset = float(assets_df.iloc[-1]["total_asset"]) if not assets_df.empty else float(args.initial_capital)
    net_pnl = pd.to_numeric(trades_df.get("net_pnl", pd.Series(dtype=float)), errors="coerce")
    wins = net_pnl > 0
    total_asset = pd.to_numeric(assets_df.get("total_asset", pd.Series(dtype=float)), errors="coerce")
    drawdown = (total_asset / total_asset.cummax() - 1) * 100 if len(total_asset) else pd.Series(dtype=float)

    rows = [
        ("start_date", args.start_date),
        ("end_date", args.end_date),
        ("fee_mode", args.fee_mode),
        ("initial_capital", float(args.initial_capital)),
        ("final_asset", final_asset),
        ("total_profit", final_asset - float(args.initial_capital)),
        ("total_return_pct", (final_asset - float(args.initial_capital)) / float(args.initial_capital) * 100),
        ("trade_count", int(len(trades_df))),
        ("win_count", int(wins.sum()) if len(wins) else 0),
        ("win_rate_pct", wins.mean() * 100 if len(wins) else 0.0),
        ("avg_trade_pnl", net_pnl.mean() if len(net_pnl) else 0.0),
        ("max_single_profit", net_pnl.max() if len(net_pnl) else 0.0),
        ("max_single_loss", net_pnl.min() if len(net_pnl) else 0.0),
        ("max_drawdown_pct", drawdown.min() if len(drawdown) else 0.0),
        ("total_buy_fee", pd.to_numeric(trades_df.get("buy_fee", pd.Series(dtype=float)), errors="coerce").sum()),
        ("total_sell_fee_tax", pd.to_numeric(trades_df.get("sell_fee_tax", pd.Series(dtype=float)), errors="coerce").sum()),
        ("total_fee_tax", pd.to_numeric(trades_df.get("total_fee_tax", pd.Series(dtype=float)), errors="coerce").sum()),
        ("skipped_count", skipped_count),
    ]
    return pd.DataFrame(rows, columns=["项目", "值"])


def build_backtest_run_args(args: argparse.Namespace, start_date: str, end_date: str) -> argparse.Namespace:
    return argparse.Namespace(
        start_date=start_date,
        end_date=end_date,
        daily_dir=vr.DAILY_REPLAY_DIR,
        selection_mode="all",
        top_n=2,
        min_rank_score=None,
        min_probability_score=None,
        min_latest_turn=args.min_latest_turn,
        max_current_drawdown=args.max_current_drawdown,
        max_latest_pct_chg=None,
        min_latest_pct_chg=None,
        max_low_to_latest_pct=None,
        max_rank=args.max_rank,
        max_daily_candidates=args.max_daily_candidates,
        max_positions=args.max_positions,
        max_daily_buys=args.max_daily_buys,
        take_profit=args.take_profit,
        stop_loss=args.stop_loss,
        max_hold_days=args.max_hold_days,
        reentry_cooldown_days=args.reentry_cooldown_days,
        initial_capital=args.initial_capital,
        lot_size=args.lot_size,
        position_sizing="all-cash",
        fee_mode=args.fee_mode,
    )


def backtest_period(args: argparse.Namespace, start_date: str, end_date: str) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    run_args = build_backtest_run_args(args, start_date, end_date)
    daily, _ = vr.load_daily_rankings(run_args)
    candidates = vr.select_portfolio_candidates(daily, run_args)
    price_cache: dict[str, pd.DataFrame] = {}
    orders, skipped_before = vr.build_portfolio_orders(candidates, price_cache)
    if args.fee_mode == "no-fees":
        trades, assets, summary = simulate_open_buy_no_fees(orders, price_cache, run_args)
    else:
        trades, assets, summary = simulate_open_buy_with_fees(orders, price_cache, run_args)
    summary.loc[len(summary)] = ["candidate_rows", int(len(candidates))]
    summary.loc[len(summary)] = ["valid_next_open_orders", int(len(orders))]
    summary.loc[len(summary)] = ["pre_trade_skipped_rows", int(len(skipped_before))]
    return trades, assets, summary


def backtest_year(args: argparse.Namespace, year: int) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    return backtest_period(args, f"{year}-01-01", f"{year}-12-31")


def summary_print_rows(all_summary: list[pd.DataFrame], label_column: str) -> pd.DataFrame:
    rows = []
    for summary in all_summary:
        label = summary[label_column].iloc[0]
        values = dict(zip(summary["项目"], summary["值"]))
        rows.append({
            label_column: label,
            "final_asset": values.get("final_asset"),
            "total_profit": values.get("total_profit"),
            "total_return_pct": values.get("total_return_pct"),
            "trade_count": values.get("trade_count"),
            "win_rate_pct": values.get("win_rate_pct"),
            "max_drawdown_pct": values.get("max_drawdown_pct"),
            "total_fee_tax": values.get("total_fee_tax"),
            "candidate_rows": values.get("candidate_rows"),
            "valid_next_open_orders": values.get("valid_next_open_orders"),
        })
    return pd.DataFrame(rows)


def safe_sheet_name(label: str, suffix: str) -> str:
    cleaned = "".join("_" if char in r'[]:*?/\\' else char for char in str(label))
    max_label_len = 31 - len(suffix) - 1
    return f"{cleaned[:max_label_len]}_{suffix}"


def parse_period_specs(specs: list[str]) -> list[tuple[str, str, str]]:
    periods: list[tuple[str, str, str]] = []
    for spec in specs:
        parts = spec.split(":")
        if len(parts) != 3:
            raise RuntimeError(f"区间格式错误: {spec}，应为 label:start_date:end_date")
        label, start_date, end_date = parts
        pd.Timestamp(start_date)
        pd.Timestamp(end_date)
        periods.append((label, start_date, end_date))
    return periods


def backtest_periods(args: argparse.Namespace) -> None:
    output = args.output or Path("a_stock_data/verification_records/portfolio_analysis/period_open_buy.xlsx")
    output.parent.mkdir(parents=True, exist_ok=True)

    periods = parse_period_specs(args.period)
    all_summary = []
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        for label, start_date, end_date in periods:
            print(f"区间回测: {label} ({start_date} 到 {end_date})")
            trades, assets, summary = backtest_period(args, start_date, end_date)
            period_summary = summary.copy()
            period_summary.insert(0, "period", label)
            all_summary.append(period_summary)
            summary.to_excel(writer, index=False, sheet_name=safe_sheet_name(label, "汇总"))
            trades.to_excel(writer, index=False, sheet_name=safe_sheet_name(label, "交易明细"))
            assets.to_excel(writer, index=False, sheet_name=safe_sheet_name(label, "每日资产"))

        combined = pd.concat(all_summary, ignore_index=True) if all_summary else pd.DataFrame()
        combined.to_excel(writer, index=False, sheet_name="区间汇总")

    print(f"已生成区间回测: {output.resolve()}")
    if all_summary:
        print(summary_print_rows(all_summary, "period").to_string(index=False))


def add_backtest_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument("--min-latest-turn", type=float, default=8.0)
    parser.add_argument("--max-current-drawdown", type=float, default=29.0)
    parser.add_argument("--max-daily-candidates", type=int, default=2)
    parser.add_argument("--max-rank", type=int, default=None)
    parser.add_argument("--max-positions", type=int, default=1)
    parser.add_argument("--max-daily-buys", type=int, default=1)
    parser.add_argument("--take-profit", type=float, default=0.08)
    parser.add_argument("--stop-loss", type=float, default=0.03)
    parser.add_argument("--max-hold-days", type=int, default=15)
    parser.add_argument("--reentry-cooldown-days", type=int, default=10)
    parser.add_argument("--initial-capital", type=float, default=50000.0)
    parser.add_argument("--lot-size", type=int, default=100)
    parser.add_argument("--fee-mode", choices=["fees", "no-fees"], default="fees")


def backtest_years(args: argparse.Namespace) -> None:
    output = args.output or Path("a_stock_data/verification_records/portfolio_analysis/yearly_open_buy_2021_2024.xlsx")
    output.parent.mkdir(parents=True, exist_ok=True)

    all_summary = []
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        for year in args.years:
            print(f"年度回测: {year}")
            trades, assets, summary = backtest_year(args, year)
            year_summary = summary.copy()
            year_summary.insert(0, "year", year)
            all_summary.append(year_summary)
            summary.to_excel(writer, index=False, sheet_name=f"{year}_汇总")
            trades.to_excel(writer, index=False, sheet_name=f"{year}_交易明细")
            assets.to_excel(writer, index=False, sheet_name=f"{year}_每日资产")

        combined = pd.concat(all_summary, ignore_index=True) if all_summary else pd.DataFrame()
        combined.to_excel(writer, index=False, sheet_name="年度汇总")

    print(f"已生成年度回测: {output.resolve()}")
    if all_summary:
        print(summary_print_rows(all_summary, "year").to_string(index=False))


def parse_years(value: str) -> list[int]:
    years: list[int] = []
    for part in value.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            start, end = part.split("-", 1)
            years.extend(range(int(start), int(end) + 1))
        else:
            years.append(int(part))
    return sorted(set(years))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="补齐 2021-2024 daily_replay 并按开盘买策略做年度回测")
    sub = parser.add_subparsers(dest="command", required=True)

    download = sub.add_parser("download-history", help="向前补齐历史 parquet 行情")
    download.add_argument("--start-date", default=DEFAULT_START_DATE)
    download.add_argument("--end-date", default=DEFAULT_END_DATE)
    download.add_argument("--force", action="store_true")
    download.add_argument("--offset", type=int, default=0)
    download.add_argument("--limit", type=int, default=None)
    download.add_argument("--sleep-min", type=float, default=0.05)
    download.add_argument("--sleep-max", type=float, default=0.15)
    download.add_argument("--max-retry", type=int, default=3)
    download.add_argument("--query-timeout", type=int, default=30)

    replay = sub.add_parser("backfill-daily", help="生成年份范围内每日回放快照")
    replay.add_argument("--years", default="2021-2024")
    replay.add_argument("--suffix", default="rebound_top3")
    replay.add_argument("--trend-threshold", type=float, default=0.07)
    replay.add_argument("--drawdown-min", type=float, default=0.27)
    replay.add_argument("--drawdown-max", type=float, default=0.33)
    replay.add_argument("--bottom-area-max", type=float, default=0.05)
    replay.add_argument("--current-rebound-max", type=float, default=0.05)
    replay.add_argument("--full-rebound-min", type=float, default=0.20)
    replay.add_argument("--min-events", type=int, default=1)
    replay.add_argument("--include-st", action="store_true")
    replay.add_argument("--progress", action="store_true")
    replay.add_argument("--verbose-errors", action="store_true")

    backtest = sub.add_parser("backtest-years", help="按次日开盘买策略做年度资金回测")
    backtest.add_argument("--years", default="2021-2024")
    add_backtest_options(backtest)

    period = sub.add_parser("backtest-periods", help="按自定义日期区间做资金回测")
    period.add_argument(
        "--period",
        action="append",
        required=True,
        help="区间格式: label:start_date:end_date，例如 2026-01-04:2026-01-01:2026-04-30",
    )
    add_backtest_options(period)

    args = parser.parse_args()
    if hasattr(args, "years"):
        args.years = parse_years(args.years)
    return args


def main() -> None:
    args = parse_args()
    if args.command == "download-history":
        download_history(args)
    elif args.command == "backfill-daily":
        backfill_daily(args)
    elif args.command == "backtest-years":
        backtest_years(args)
    elif args.command == "backtest-periods":
        backtest_periods(args)


if __name__ == "__main__":
    main()
