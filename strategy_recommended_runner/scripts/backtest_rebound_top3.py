from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

from filter_drawdown_rebound_history import (
    PARQUET_DIR,
    Event,
    Pivot,
    find_pivots,
    is_st_stock,
    load_stock_name_map,
    max_high_between,
    normalize_code,
    stock_result_row,
)
from rank_rebound_candidates import safe_float


OUT_FILE = Path("rebound_top3_backtest_2026_04.xlsx")


@dataclass(frozen=True)
class StockData:
    code: str
    name: str
    df: pd.DataFrame
    pivots: list[Pivot]
    date_to_pos: dict[pd.Timestamp, int]


def load_raw_price_frame(path: Path) -> pd.DataFrame:
    columns = ["date", "high", "low", "close", "pct_chg", "turn"]
    df = pd.read_parquet(path, columns=columns)
    if df.empty:
        return df

    df = df.copy()
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    for column in ["high", "low", "close", "pct_chg", "turn"]:
        df[column] = pd.to_numeric(df[column], errors="coerce")

    df = df.dropna(subset=["date", "high", "low", "close"])
    return df.sort_values("date").reset_index(drop=True)


def signal_price_frame(df: pd.DataFrame, end_date: pd.Timestamp | None = None) -> pd.DataFrame:
    frame = df.loc[:, ["date", "high", "low", "close"]].copy()
    if end_date is not None:
        frame = frame[frame["date"] <= end_date]
    return frame.reset_index(drop=True)


def signal_price_frame_until_pos(df: pd.DataFrame, end_pos: int) -> pd.DataFrame:
    frame = df.iloc[: end_pos + 1].loc[:, ["date", "high", "low", "close"]].copy()
    return frame.reset_index(drop=True)


def build_date_position_index(df: pd.DataFrame) -> dict[pd.Timestamp, int]:
    return {pd.Timestamp(date): int(pos) for pos, date in enumerate(df["date"])}


def load_stock_data(args: argparse.Namespace) -> list[StockData]:
    if not PARQUET_DIR.exists():
        raise FileNotFoundError(f"没有找到数据目录: {PARQUET_DIR}")

    name_map = load_stock_name_map()
    stocks: list[StockData] = []

    parquet_files = sorted(PARQUET_DIR.glob("*.parquet"))
    if not parquet_files:
        raise FileNotFoundError(f"没有找到 parquet 文件: {PARQUET_DIR}")

    for number, path in enumerate(parquet_files, start=1):
        code = normalize_code(path.stem)
        name = name_map.get(code, "")
        if not args.include_st and is_st_stock(name):
            continue

        try:
            df = load_raw_price_frame(path)
        except Exception as exc:
            print(f"读取失败: {path}, {type(exc).__name__}: {exc}")
            continue

        if not df.empty:
            pivot_frame = signal_price_frame(df)
            pivots = find_pivots(pivot_frame, args.trend_threshold)
            stocks.append(
                StockData(
                    code=code,
                    name=name,
                    df=df,
                    pivots=pivots,
                    date_to_pos=build_date_position_index(df),
                )
            )

        if args.progress and number % 500 == 0:
            print(f"已加载 {number}/{len(parquet_files)}，有效 {len(stocks)} 只")

    return stocks


def price_frame_until(df: pd.DataFrame, replay_date: pd.Timestamp) -> pd.DataFrame:
    return signal_price_frame(df, replay_date)


def latest_row_on(df: pd.DataFrame, replay_date: pd.Timestamp) -> pd.Series | None:
    rows = df[df["date"] == replay_date]
    if rows.empty:
        return None
    return rows.iloc[-1]


def known_pivots(stock: StockData, replay_date: pd.Timestamp) -> list[Pivot]:
    return [
        pivot
        for pivot in stock.pivots
        if pivot.confirm_date <= replay_date
    ]


def build_events_from_pivots(df: pd.DataFrame, pivots: list[Pivot]) -> list[Event]:
    events: list[Event] = []

    for i, pivot in enumerate(pivots):
        if pivot.kind != "top":
            continue

        next_bottom: Pivot | None = None
        next_top: Pivot | None = None

        for candidate in pivots[i + 1:]:
            if next_bottom is None:
                if candidate.kind == "bottom":
                    next_bottom = candidate
                continue

            if candidate.kind == "top":
                next_top = candidate
                break

        if next_bottom is None:
            continue

        drawdown_pct = (pivot.price - next_bottom.price) / pivot.price

        if next_top is None:
            rebound_high_date, rebound_high = max_high_between(df, next_bottom.row_index)
            rebound_ended = False
        else:
            rebound_high_date = next_top.date
            rebound_high = next_top.price
            rebound_ended = True

        rebound_pct = (rebound_high - next_bottom.price) / next_bottom.price
        latest_close = float(df.iloc[-1]["close"])
        latest_close_rebound_pct = (latest_close - next_bottom.price) / next_bottom.price

        events.append(Event(
            top_date=pivot.date,
            top_price=pivot.price,
            top_confirm_date=pivot.confirm_date,
            top_confirm_price=pivot.confirm_price,
            low_date=next_bottom.date,
            low_price=next_bottom.price,
            low_confirm_date=next_bottom.confirm_date,
            low_confirm_price=next_bottom.confirm_price,
            drawdown_pct=drawdown_pct,
            rebound_high_date=rebound_high_date,
            rebound_high=rebound_high,
            rebound_pct=rebound_pct,
            latest_close_rebound_pct=latest_close_rebound_pct,
            rebound_ended=rebound_ended,
            low_confirmed=True,
        ))

    return events


def build_current_event_from_pivots(df: pd.DataFrame, pivots: list[Pivot]) -> Event | None:
    last_top: Pivot | None = None
    last_top_index = -1

    for i, pivot in enumerate(pivots):
        if pivot.kind == "top":
            last_top = pivot
            last_top_index = i

    if last_top is None:
        return None

    next_bottom: Pivot | None = None
    for pivot in pivots[last_top_index + 1:]:
        if pivot.kind == "bottom":
            next_bottom = pivot
            break

    if next_bottom is None:
        span = df.iloc[last_top.row_index:]
        low_idx = span["low"].idxmin()
        low_date = pd.Timestamp(df.loc[low_idx, "date"])
        low_price = float(df.loc[low_idx, "low"])
        low_confirm_date = None
        low_confirm_price = None
        low_confirmed = False
        low_row_index = int(low_idx)
    else:
        low_date = next_bottom.date
        low_price = next_bottom.price
        low_confirm_date = next_bottom.confirm_date
        low_confirm_price = next_bottom.confirm_price
        low_confirmed = True
        low_row_index = next_bottom.row_index

    drawdown_pct = (last_top.price - low_price) / last_top.price
    rebound_high_date, rebound_high = max_high_between(df, low_row_index)
    rebound_pct = (rebound_high - low_price) / low_price
    latest_close = float(df.iloc[-1]["close"])
    latest_close_rebound_pct = (latest_close - low_price) / low_price

    return Event(
        top_date=last_top.date,
        top_price=last_top.price,
        top_confirm_date=last_top.confirm_date,
        top_confirm_price=last_top.confirm_price,
        low_date=low_date,
        low_price=low_price,
        low_confirm_date=low_confirm_date,
        low_confirm_price=low_confirm_price,
        drawdown_pct=drawdown_pct,
        rebound_high_date=rebound_high_date,
        rebound_high=rebound_high,
        rebound_pct=rebound_pct,
        latest_close_rebound_pct=latest_close_rebound_pct,
        rebound_ended=False,
        low_confirmed=low_confirmed,
    )


def previous_same_drawdown_events(
    events: list[Event],
    current: Event,
    args: argparse.Namespace,
) -> tuple[list[Event], list[Event]]:
    previous = [
        event
        for event in events
        if event.top_date < current.top_date
        and args.drawdown_min <= event.drawdown_pct <= args.drawdown_max
    ]
    success = [
        event
        for event in previous
        if event.rebound_pct >= args.full_rebound_min
    ]
    return previous, success


def score_row(
    row: dict[str, object],
    previous_same_drawdown: list[Event],
    previous_success: list[Event],
    latest: pd.Series,
    args: argparse.Namespace,
) -> dict[str, object]:
    sample_count = len(previous_same_drawdown)
    success_count = len(previous_success)
    success_rate = success_count / sample_count if sample_count else 0.0
    avg_rebound = sum(event.rebound_pct for event in previous_success) / success_count if success_count else 0.0
    max_rebound = max((event.rebound_pct for event in previous_success), default=0.0)

    latest_pct_chg = safe_float(latest.get("pct_chg", latest.get("pctChg", 0)))
    latest_turn = safe_float(latest.get("turn", 0))

    current_low_to_latest = safe_float(row["current_low_to_latest_pct"]) / 100
    current_low_to_high = safe_float(row["current_low_to_high_pct"]) / 100
    current_drawdown = safe_float(row["current_drawdown_pct"]) / 100
    current_top_price = safe_float(row.get("current_top_price"))
    latest_close = safe_float(row.get("latest_close"))
    close_drawdown = (
        (current_top_price - latest_close) / current_top_price
        if current_top_price > 0
        else float("nan")
    )
    historical_count = int(row["historical_valid_event_count"])
    bottom_confirmed = bool(row["current_is_bottom_confirmed"])
    stage = str(row["current_stage"])

    sample_score = min(sample_count, 4) / 4
    history_score = 0.65 * success_rate + 0.20 * sample_score + 0.15 * min(avg_rebound / 0.50, 1.0)

    cheap_score = max(0.0, min(1.0, (args.current_rebound_max - current_low_to_latest) / args.current_rebound_max))
    if current_low_to_latest <= 0:
        momentum_band_score = 0.15
    elif current_low_to_latest <= 0.01:
        momentum_band_score = 0.35
    elif current_low_to_latest <= 0.05:
        momentum_band_score = 1.0
    elif current_low_to_latest <= args.current_rebound_max:
        momentum_band_score = 0.75
    else:
        momentum_band_score = 0.0

    if 0 <= latest_pct_chg <= 5:
        day_momentum_score = 1.0
    elif 5 < latest_pct_chg <= 8:
        day_momentum_score = 0.65
    elif -3 <= latest_pct_chg < 0:
        day_momentum_score = 0.45
    else:
        day_momentum_score = 0.2

    confirm_score = 1.0 if bottom_confirmed else 0.55
    stage_score = {"bottom_area": 0.85, "just_rebound": 1.0, "confirmed_rebound": 0.35}.get(stage, 0.5)
    current_score = (
        0.30 * cheap_score
        + 0.30 * momentum_band_score
        + 0.20 * day_momentum_score
        + 0.10 * confirm_score
        + 0.10 * stage_score
    )

    probability_score = 100 * (0.62 * history_score + 0.38 * current_score)
    upside_to_20 = max(0.0, args.full_rebound_min - current_low_to_latest)
    risk_to_low = max(0.0, current_low_to_latest)
    rank_score = probability_score * (1 + upside_to_20) / (1 + risk_to_low)

    return {
        "rank_score": rank_score,
        "probability_score": probability_score,
        "code": row["code"],
        "name": row["name"],
        "current_stage": stage,
        "latest_date": row["latest_date"],
        "latest_close": row["latest_close"],
        "current_top_price": current_top_price,
        "current_low_date": row["current_low_date"],
        "current_low_price": row["current_low_price"],
        "current_low_to_latest_pct": current_low_to_latest * 100,
        "current_low_to_high_pct": current_low_to_high * 100,
        "current_drawdown_pct": current_drawdown * 100,
        "close_drawdown_pct": close_drawdown * 100,
        "current_is_bottom_confirmed": bottom_confirmed,
        "latest_pct_chg": latest_pct_chg,
        "latest_turn": latest_turn,
        "historical_valid_event_count": historical_count,
        "history_sample_count": sample_count,
        "history_success_count": success_count,
        "history_success_rate_pct": success_rate * 100,
        "history_avg_rebound_pct": avg_rebound * 100,
        "history_max_rebound_pct": max_rebound * 100,
        "upside_to_20pct_target_pct": upside_to_20 * 100,
        "risk_back_to_low_pct": risk_to_low * 100,
        "reason": (
            f"历史{success_count}/{sample_count}次成功; "
            f"当前距低点{current_low_to_latest * 100:.2f}%; "
            f"最新日涨幅{latest_pct_chg:.2f}%; "
            f"状态{stage}; "
            f"{'底部已确认' if bottom_confirmed else '底部未确认'}"
        ),
    }


def replay_day(stocks: list[StockData], replay_date: pd.Timestamp, args: argparse.Namespace) -> pd.DataFrame:
    rows: list[dict[str, object]] = []

    for stock in stocks:
        latest_pos = stock.date_to_pos.get(replay_date)
        if latest_pos is None:
            continue

        latest = stock.df.iloc[latest_pos]
        df = signal_price_frame_until_pos(stock.df, latest_pos)
        if df.empty:
            continue

        try:
            pivots = known_pivots(stock, replay_date)
            events = build_events_from_pivots(df, pivots)
            current = build_current_event_from_pivots(df, pivots)
            screening_row = stock_result_row(
                code=stock.code,
                name=stock.name,
                df=df,
                events=events,
                current=current,
                drawdown_min=args.drawdown_min,
                drawdown_max=args.drawdown_max,
                full_rebound_min=args.full_rebound_min,
                bottom_area_max=args.bottom_area_max,
                current_rebound_max=args.current_rebound_max,
                min_events=args.min_events,
            )
        except Exception as exc:
            if args.verbose_errors:
                print(f"回放失败: {replay_date:%Y-%m-%d} {stock.code}, {type(exc).__name__}: {exc}")
            continue

        if screening_row is None or current is None:
            continue

        previous_same_drawdown, previous_success = previous_same_drawdown_events(events, current, args)
        scored = score_row(screening_row, previous_same_drawdown, previous_success, latest, args)
        rows.append(scored)

    if not rows:
        return pd.DataFrame()

    ranking = pd.DataFrame(rows).sort_values(
        ["rank_score", "probability_score"],
        ascending=False,
    ).reset_index(drop=True)
    ranking.insert(0, "rank", range(1, len(ranking) + 1))
    return ranking


def replay_dates(stocks: list[StockData], args: argparse.Namespace) -> list[pd.Timestamp]:
    start = pd.Timestamp(args.start_date)
    end = pd.Timestamp(args.end_date)
    dates: set[pd.Timestamp] = set()

    for stock in stocks:
        mask = (stock.df["date"] >= start) & (stock.df["date"] <= end)
        dates.update(pd.Timestamp(date) for date in stock.df.loc[mask, "date"])

    return sorted(dates)


def future_window(df: pd.DataFrame, signal_date: pd.Timestamp, lookahead_days: int) -> pd.DataFrame:
    future = df[df["date"] > signal_date].head(lookahead_days).copy()
    return future.reset_index(drop=True)


def verify_signal(signal: pd.Series, stock: StockData, args: argparse.Namespace) -> dict[str, object]:
    signal_date = pd.Timestamp(signal["signal_date"])
    buy_close = float(signal["latest_close"])
    future = future_window(stock.df, signal_date, args.lookahead_days)

    out = signal.to_dict()
    out["buy_close"] = buy_close
    out["lookahead_days"] = int(args.lookahead_days)

    if future.empty:
        out.update({
            "verify_end_date": "",
            "future_trade_days": 0,
            "max_high_date": "",
            "max_high_after_signal": None,
            "max_gain_pct": 0.0,
            "hit_success_threshold": False,
            "min_low_date": "",
            "min_low_after_signal": None,
            "max_drawdown_pct": 0.0,
            "verify_close": None,
            "close_return_pct": None,
        })
        return out

    max_high_idx = future["high"].idxmax()
    min_low_idx = future["low"].idxmin()
    max_high = float(future.loc[max_high_idx, "high"])
    min_low = float(future.loc[min_low_idx, "low"])
    verify_close = float(future.iloc[-1]["close"])

    out.update({
        "verify_end_date": pd.Timestamp(future.iloc[-1]["date"]).strftime("%Y-%m-%d"),
        "future_trade_days": int(len(future)),
        "max_high_date": pd.Timestamp(future.loc[max_high_idx, "date"]).strftime("%Y-%m-%d"),
        "max_high_after_signal": max_high,
        "max_gain_pct": (max_high - buy_close) / buy_close * 100,
        "hit_success_threshold": (max_high - buy_close) / buy_close >= args.success_threshold,
        "min_low_date": pd.Timestamp(future.loc[min_low_idx, "date"]).strftime("%Y-%m-%d"),
        "min_low_after_signal": min_low,
        "max_drawdown_pct": (min_low - buy_close) / buy_close * 100,
        "verify_close": verify_close,
        "close_return_pct": (verify_close - buy_close) / buy_close * 100,
    })
    return out


def deduplicate_signals(daily_top: pd.DataFrame) -> pd.DataFrame:
    if daily_top.empty:
        return daily_top

    deduped = daily_top.sort_values(
        ["signal_date", "rank"],
        ascending=[True, True],
    ).drop_duplicates(subset=["code"], keep="first")
    return deduped.reset_index(drop=True)


def build_summary(verified: pd.DataFrame, daily_top: pd.DataFrame, args: argparse.Namespace) -> pd.DataFrame:
    hits = verified["hit_success_threshold"].fillna(False).astype(bool) if "hit_success_threshold" in verified else pd.Series(dtype=bool)
    max_gain = pd.to_numeric(verified.get("max_gain_pct", pd.Series(dtype=float)), errors="coerce")
    max_drawdown = pd.to_numeric(verified.get("max_drawdown_pct", pd.Series(dtype=float)), errors="coerce")

    rows = [
        ("start_date", args.start_date),
        ("end_date", args.end_date),
        ("top_n_per_day", args.top_n),
        ("daily_signal_rows", int(len(daily_top))),
        ("deduped_stock_rows", int(len(verified))),
        ("success_threshold_pct", args.success_threshold * 100),
        ("lookahead_trade_days", args.lookahead_days),
        ("success_count", int(hits.sum())),
        ("accuracy_pct", hits.mean() * 100 if len(hits) else 0.0),
        ("avg_max_gain_pct", max_gain.mean()),
        ("median_max_gain_pct", max_gain.median()),
        ("avg_max_drawdown_pct", max_drawdown.mean()),
        ("current_rebound_max_pct", args.current_rebound_max * 100),
        ("drawdown_min_pct", args.drawdown_min * 100),
        ("drawdown_max_pct", args.drawdown_max * 100),
        ("trend_threshold_pct", args.trend_threshold * 100),
        ("full_rebound_min_pct", args.full_rebound_min * 100),
        ("include_st", bool(args.include_st)),
        ("dedupe_policy", "same code keeps first April signal"),
        ("success_base", "signal day close to next 10 trading days high"),
    ]
    return pd.DataFrame(rows, columns=["项目", "值"])


def explanation_frame(args: argparse.Namespace) -> pd.DataFrame:
    rows = [
        ("用途", "回放指定日期范围内每个交易日的模型 Top3，并验证后续表现。"),
        ("查找约束", f"每个回放日只使用当日及以前行情；当前回撤 {args.drawdown_min * 100:.0f}%-{args.drawdown_max * 100:.0f}%；当日收盘距低点 <= {args.current_rebound_max * 100:.0f}%。"),
        ("排序约束", f"每天按 rank_score、probability_score 降序，只保留 Top {args.top_n} 进入原始信号表。"),
        ("去重约束", "同一股票在回测区间多次入选时，只保留首次入选。"),
        ("成功标准", f"以入选日收盘价为基准，之后 {args.lookahead_days} 个交易日内最高价涨幅 >= {args.success_threshold * 100:.0f}% 算成功。"),
        ("注意", "后续行情只用于验证，不参与当日筛选和排名。"),
    ]
    return pd.DataFrame(rows, columns=["项目", "说明"])


def write_excel(
    summary: pd.DataFrame,
    verified: pd.DataFrame,
    daily_top: pd.DataFrame,
    args: argparse.Namespace,
) -> None:
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with pd.ExcelWriter(args.output) as writer:
        summary.to_excel(writer, index=False, sheet_name="汇总")
        verified.to_excel(writer, index=False, sheet_name="入选明细")
        daily_top.to_excel(writer, index=False, sheet_name="每日Top3")
        explanation_frame(args).to_excel(writer, index=False, sheet_name="参数说明")


def run_backtest(args: argparse.Namespace) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    stocks = load_stock_data(args)
    if not stocks:
        raise RuntimeError("没有可用股票数据")

    stock_map = {stock.code: stock for stock in stocks}
    all_daily_rows: list[pd.DataFrame] = []
    dates = replay_dates(stocks, args)
    if not dates:
        raise RuntimeError(f"{args.start_date} 到 {args.end_date} 没有交易日数据")

    for number, replay_date in enumerate(dates, start=1):
        ranking = replay_day(stocks, replay_date, args)
        if not ranking.empty:
            top = ranking.head(args.top_n).copy()
            top.insert(0, "signal_date", replay_date.strftime("%Y-%m-%d"))
            all_daily_rows.append(top)

        if args.progress:
            print(f"已回放 {number}/{len(dates)} {replay_date:%Y-%m-%d}，当日候选 {len(ranking)}")

    if all_daily_rows:
        daily_top = pd.concat(all_daily_rows, ignore_index=True)
    else:
        daily_top = pd.DataFrame()

    deduped = deduplicate_signals(daily_top)
    verified_rows = []
    for _, signal in deduped.iterrows():
        stock = stock_map.get(normalize_code(signal["code"]))
        if stock is None:
            continue
        verified_rows.append(verify_signal(signal, stock, args))

    verified = pd.DataFrame(verified_rows)
    summary = build_summary(verified, daily_top, args)
    return summary, verified, daily_top


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="回放指定月份的反弹模型 Top3，并验证后续 10 个交易日是否涨超 15%。")
    parser.add_argument("--start-date", default="2026-04-01", help="回测开始日期，默认 2026-04-01")
    parser.add_argument("--end-date", default="2026-04-30", help="回测结束日期，默认 2026-04-30")
    parser.add_argument("--top-n", type=int, default=3, help="每日只验证排行前 N 个，默认 3")
    parser.add_argument("--lookahead-days", type=int, default=10, help="入选后观察交易日数量，默认 10")
    parser.add_argument("--success-threshold", type=float, default=0.15, help="成功涨幅阈值，默认 0.15")
    parser.add_argument("--trend-threshold", type=float, default=0.07, help="趋势反转阈值，默认 0.07")
    parser.add_argument("--drawdown-min", type=float, default=0.27, help="回撤下限，默认 0.27")
    parser.add_argument("--drawdown-max", type=float, default=0.33, help="回撤上限，默认 0.33")
    parser.add_argument("--bottom-area-max", type=float, default=0.05, help="bottom_area 分界，默认 0.05")
    parser.add_argument("--current-rebound-max", type=float, default=0.05, help="当日收盘距当前低点最大涨幅，默认 0.05")
    parser.add_argument("--full-rebound-min", type=float, default=0.20, help="历史有效事件反弹下限，默认 0.20")
    parser.add_argument("--min-events", type=int, default=1, help="至少出现的历史有效事件次数，默认 1")
    parser.add_argument("--output", type=Path, default=OUT_FILE, help=f"输出 Excel，默认 {OUT_FILE}")
    parser.add_argument("--include-st", action="store_true", help="包含名称中带 ST 的股票；默认排除")
    parser.add_argument("--progress", action="store_true", help="打印加载和回放进度")
    parser.add_argument("--verbose-errors", action="store_true", help="打印单只股票回放错误")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    summary, verified, daily_top = run_backtest(args)
    write_excel(summary, verified, daily_top, args)

    print(summary.to_string(index=False))
    print()
    if not verified.empty:
        print(verified[[
            "signal_date",
            "rank",
            "code",
            "name",
            "buy_close",
            "max_gain_pct",
            "hit_success_threshold",
        ]].to_string(index=False, max_colwidth=80))
        print()
    print(f"已导出: {args.output.resolve()}")


if __name__ == "__main__":
    main()
