from __future__ import annotations

import argparse
from dataclasses import dataclass
from html import escape
from pathlib import Path

import pandas as pd


BASE_DIR = Path("a_stock_data")
PARQUET_DIR = BASE_DIR / "parquet"
META_DIR = BASE_DIR / "meta"
CHART_DIR = BASE_DIR / "drawdown_rebound_charts"

STOCK_LIST_FILE = META_DIR / "stock_list.csv"
OUT_FILE = Path("drawdown_rebound_history_result.xlsx")


@dataclass(frozen=True)
class Pivot:
    kind: str
    date: pd.Timestamp
    price: float
    confirm_date: pd.Timestamp
    confirm_price: float
    row_index: int


@dataclass(frozen=True)
class Event:
    top_date: pd.Timestamp
    top_price: float
    top_confirm_date: pd.Timestamp
    top_confirm_price: float
    low_date: pd.Timestamp
    low_price: float
    low_confirm_date: pd.Timestamp | None
    low_confirm_price: float | None
    drawdown_pct: float
    rebound_high_date: pd.Timestamp
    rebound_high: float
    rebound_pct: float
    latest_close_rebound_pct: float
    rebound_ended: bool
    low_confirmed: bool


def normalize_code(code) -> str:
    code = str(code).strip()
    if "." in code:
        code = code.split(".")[-1]
    return code.zfill(6)


def load_stock_name_map() -> dict[str, str]:
    if not STOCK_LIST_FILE.exists():
        return {}

    stock_list = pd.read_csv(STOCK_LIST_FILE, dtype=str)

    if "code" in stock_list.columns and "name" in stock_list.columns:
        code_col = "code"
        name_col = "name"
    elif "代码" in stock_list.columns and "名称" in stock_list.columns:
        code_col = "代码"
        name_col = "名称"
    else:
        raise RuntimeError(f"stock_list.csv 字段异常: {list(stock_list.columns)}")

    stock_list[code_col] = stock_list[code_col].apply(normalize_code)
    stock_list[name_col] = stock_list[name_col].astype(str)

    return dict(zip(stock_list[code_col], stock_list[name_col]))


def is_st_stock(name: str) -> bool:
    return "ST" in str(name).upper()


def load_price_frame(path: Path) -> pd.DataFrame:
    df = pd.read_parquet(path, columns=["date", "high", "low", "close"])
    if df.empty:
        return df

    df = df.copy()
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    for column in ["high", "low", "close"]:
        df[column] = pd.to_numeric(df[column], errors="coerce")

    df = df.dropna(subset=["date", "high", "low", "close"])
    df = df.sort_values("date").reset_index(drop=True)
    return df


def find_pivots(df: pd.DataFrame, trend_threshold: float) -> list[Pivot]:
    if df.empty:
        return []

    trend: str | None = None
    low_i = high_i = 0
    low_p = float(df.loc[0, "low"])
    high_p = float(df.loc[0, "high"])
    pivots: list[Pivot] = []

    for i, row in df.iterrows():
        high = float(row["high"])
        low = float(row["low"])
        date = pd.Timestamp(row["date"])

        if trend is None:
            if low < low_p:
                low_p = low
                low_i = i
            if high > high_p:
                high_p = high
                high_i = i

            if high >= low_p * (1 + trend_threshold):
                pivots.append(Pivot(
                    kind="bottom",
                    date=pd.Timestamp(df.loc[low_i, "date"]),
                    price=low_p,
                    confirm_date=date,
                    confirm_price=high,
                    row_index=low_i,
                ))
                trend = "up"
                high_p = high
                high_i = i
            elif low <= high_p * (1 - trend_threshold):
                pivots.append(Pivot(
                    kind="top",
                    date=pd.Timestamp(df.loc[high_i, "date"]),
                    price=high_p,
                    confirm_date=date,
                    confirm_price=low,
                    row_index=high_i,
                ))
                trend = "down"
                low_p = low
                low_i = i

        elif trend == "up":
            if high > high_p:
                high_p = high
                high_i = i
            elif low <= high_p * (1 - trend_threshold):
                pivots.append(Pivot(
                    kind="top",
                    date=pd.Timestamp(df.loc[high_i, "date"]),
                    price=high_p,
                    confirm_date=date,
                    confirm_price=low,
                    row_index=high_i,
                ))
                trend = "down"
                low_p = low
                low_i = i

        else:
            if low < low_p:
                low_p = low
                low_i = i
            elif high >= low_p * (1 + trend_threshold):
                pivots.append(Pivot(
                    kind="bottom",
                    date=pd.Timestamp(df.loc[low_i, "date"]),
                    price=low_p,
                    confirm_date=date,
                    confirm_price=high,
                    row_index=low_i,
                ))
                trend = "up"
                high_p = high
                high_i = i

    return pivots


def max_high_between(df: pd.DataFrame, start_i: int, end_i: int | None = None) -> tuple[pd.Timestamp, float]:
    if end_i is None:
        span = df.iloc[start_i:]
    else:
        span = df.iloc[start_i:end_i + 1]

    high_idx = span["high"].idxmax()
    return pd.Timestamp(df.loc[high_idx, "date"]), float(df.loc[high_idx, "high"])


def build_events(df: pd.DataFrame, trend_threshold: float) -> list[Event]:
    pivots = find_pivots(df, trend_threshold)
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


def build_current_event(df: pd.DataFrame, trend_threshold: float) -> Event | None:
    pivots = find_pivots(df, trend_threshold)
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


def event_is_valid(
    event: Event,
    drawdown_min: float,
    drawdown_max: float,
    full_rebound_min: float,
) -> bool:
    return (
        drawdown_min <= event.drawdown_pct <= drawdown_max
        and event.rebound_pct >= full_rebound_min
    )


def format_event(event: Event) -> str:
    return (
        f"{event.top_date:%Y-%m-%d} {event.top_price:.2f}"
        f" -> {event.low_date:%Y-%m-%d} {event.low_price:.2f}"
        f" dd={event.drawdown_pct * 100:.2f}%"
        f" -> {event.rebound_high_date:%Y-%m-%d} {event.rebound_high:.2f}"
        f" rb={event.rebound_pct * 100:.2f}%"
    )


def stock_result_row(
    code: str,
    name: str,
    df: pd.DataFrame,
    events: list[Event],
    current: Event | None,
    drawdown_min: float,
    drawdown_max: float,
    full_rebound_min: float,
    bottom_area_max: float,
    current_rebound_max: float,
    min_events: int,
) -> dict[str, object] | None:
    if current is None:
        return None

    latest_date = pd.Timestamp(df.iloc[-1]["date"])
    latest_close = float(df.iloc[-1]["close"])

    current_valid = (
        drawdown_min <= current.drawdown_pct <= drawdown_max
        and current.rebound_pct >= 0
        and current.latest_close_rebound_pct >= 0
        and current.latest_close_rebound_pct <= current_rebound_max
    )
    if not current_valid:
        return None

    previous_valid_events = [
        event
        for event in events
        if event_is_valid(event, drawdown_min, drawdown_max, full_rebound_min)
        and event.top_date < current.top_date
    ]
    if len(previous_valid_events) < min_events:
        return None

    if current.rebound_pct >= full_rebound_min:
        current_stage = "confirmed_rebound"
    elif current.latest_close_rebound_pct <= bottom_area_max:
        current_stage = "bottom_area"
    else:
        current_stage = "just_rebound"

    return {
        "code": code,
        "name": name,
        "latest_date": latest_date.strftime("%Y-%m-%d"),
        "latest_close": latest_close,
        "historical_valid_event_count": len(previous_valid_events),
        "previous_event_count": len(previous_valid_events),
        "current_stage": current_stage,
        "current_is_bottom_confirmed": current.low_confirmed,
        "current_top_date": current.top_date.strftime("%Y-%m-%d"),
        "current_top_price": current.top_price,
        "current_top_confirm_date": current.top_confirm_date.strftime("%Y-%m-%d"),
        "current_top_confirm_price": current.top_confirm_price,
        "current_low_date": current.low_date.strftime("%Y-%m-%d"),
        "current_low_price": current.low_price,
        "current_low_confirm_date": "" if current.low_confirm_date is None else current.low_confirm_date.strftime("%Y-%m-%d"),
        "current_low_confirm_price": current.low_confirm_price,
        "current_drawdown_pct": current.drawdown_pct * 100,
        "current_rebound_high_date": current.rebound_high_date.strftime("%Y-%m-%d"),
        "current_rebound_high": current.rebound_high,
        "current_rebound_pct": current.rebound_pct * 100,
        "latest_close_rebound_pct": current.latest_close_rebound_pct * 100,
        "current_low_to_high_pct": current.rebound_pct * 100,
        "current_low_to_latest_pct": current.latest_close_rebound_pct * 100,
        "previous_events": "; ".join(format_event(event) for event in previous_valid_events),
        "_events": previous_valid_events + [current],
    }


def make_svg_chart(code: str, name: str, df: pd.DataFrame, events: list[Event], out_path: Path) -> None:
    current = events[-1]
    start_date = min(event.top_date for event in events)
    view = df[df["date"] >= start_date].reset_index(drop=True)
    if view.empty:
        return

    width = 1200
    height = 660
    margin_left = 75
    margin_right = 30
    margin_top = 45
    margin_bottom = 95
    plot_width = width - margin_left - margin_right
    plot_height = height - margin_top - margin_bottom

    y_min = float(view["low"].min()) * 0.96
    y_max = float(view["high"].max()) * 1.04

    def x_pos(i: int) -> float:
        if len(view) <= 1:
            return margin_left + plot_width / 2
        return margin_left + i / (len(view) - 1) * plot_width

    def y_pos(value: float) -> float:
        return margin_top + (y_max - value) / (y_max - y_min) * plot_height

    date_to_index = {pd.Timestamp(row["date"]).date(): i for i, row in view.iterrows()}
    close_points = " ".join(
        f"{x_pos(i):.1f},{y_pos(float(row['close'])):.1f}"
        for i, row in view.iterrows()
    )

    svg: list[str] = []
    svg.append(f"<svg xmlns='http://www.w3.org/2000/svg' width='{width}' height='{height}' viewBox='0 0 {width} {height}'>")
    svg.append("<rect width='100%' height='100%' fill='white'/>")
    svg.append(
        f"<text x='{margin_left}' y='28' font-family='Arial' font-size='18' font-weight='700'>"
        f"{escape(code)} {escape(name)} drawdown/rebound history</text>"
    )

    for k in range(6):
        value = y_min + (y_max - y_min) * k / 5
        yy = y_pos(value)
        svg.append(f"<line x1='{margin_left}' y1='{yy:.1f}' x2='{width - margin_right}' y2='{yy:.1f}' stroke='#e5e7eb'/>")
        svg.append(
            f"<text x='{margin_left - 8}' y='{yy + 4:.1f}' text-anchor='end' "
            f"font-family='Arial' font-size='11' fill='#555'>{value:.2f}</text>"
        )

    for i, row in view.iterrows():
        xx = x_pos(i)
        svg.append(
            f"<line x1='{xx:.1f}' y1='{y_pos(float(row['low'])):.1f}' "
            f"x2='{xx:.1f}' y2='{y_pos(float(row['high'])):.1f}' stroke='#c7c7c7' stroke-width='1'/>"
        )
    svg.append(f"<polyline points='{close_points}' fill='none' stroke='#1f77b4' stroke-width='2.1'/>")

    for event_no, event in enumerate(events, start=1):
        color = "#d62728" if event is current else "#6b7280"
        low_color = "#2ca02c" if event is current else "#9ca3af"
        rebound_color = "#9467bd" if event is current else "#64748b"

        for label, date, price, marker_color, dy in [
            (f"E{event_no} top", event.top_date, event.top_price, color, -14),
            (f"E{event_no} low", event.low_date, event.low_price, low_color, 18),
            (f"E{event_no} rebound", event.rebound_high_date, event.rebound_high, rebound_color, -14),
        ]:
            idx = date_to_index.get(pd.Timestamp(date).date())
            if idx is None:
                continue
            xx = x_pos(idx)
            yy = y_pos(price)
            svg.append(f"<circle cx='{xx:.1f}' cy='{yy:.1f}' r='5' fill='{marker_color}'/>")
            svg.append(
                f"<text x='{xx + 7:.1f}' y='{yy + dy:.1f}' font-family='Arial' "
                f"font-size='11' fill='{marker_color}'>{escape(label)} {price:.2f}</text>"
            )

    threshold_lines = [
        ("current top -7%", current.top_price * 0.93, "#ff7f0e"),
        ("current top -30%", current.top_price * 0.70, "#d62728"),
        ("current low +20%", current.low_price * 1.20, "#9467bd"),
    ]
    for label, value, color in threshold_lines:
        yy = y_pos(value)
        svg.append(
            f"<line x1='{margin_left}' y1='{yy:.1f}' x2='{width - margin_right}' y2='{yy:.1f}' "
            f"stroke='{color}' stroke-dasharray='6 5' opacity='0.65'/>"
        )
        svg.append(
            f"<text x='{width - margin_right - 5}' y='{yy - 5:.1f}' text-anchor='end' "
            f"font-family='Arial' font-size='12' fill='{color}'>{escape(label)} {value:.2f}</text>"
        )

    step = max(1, len(view) // 8)
    for i in range(0, len(view), step):
        xx = x_pos(i)
        date_label = pd.Timestamp(view.loc[i, "date"]).date()
        svg.append(
            f"<text x='{xx:.1f}' y='{height - 50}' transform='rotate(35 {xx:.1f},{height - 50})' "
            f"font-family='Arial' font-size='11' fill='#555'>{date_label}</text>"
        )

    svg.append(
        f"<line x1='{margin_left}' y1='{margin_top}' x2='{margin_left}' y2='{height - margin_bottom}' stroke='#333'/>"
        f"<line x1='{margin_left}' y1='{height - margin_bottom}' x2='{width - margin_right}' y2='{height - margin_bottom}' stroke='#333'/>"
    )
    svg.append(
        "<text x='75' y='625' font-family='Arial' font-size='12' fill='#555'>"
        "Gray vertical lines = daily high/low, blue line = close. History requires +20%; current event is classified as bottom area, just rebound, or confirmed rebound.</text>"
    )
    svg.append("</svg>")

    out_path.write_text("\n".join(svg), encoding="utf-8")


def scan_stocks(args: argparse.Namespace) -> pd.DataFrame:
    if not PARQUET_DIR.exists():
        raise FileNotFoundError(f"没有找到数据目录: {PARQUET_DIR}")

    name_map = load_stock_name_map()
    rows: list[dict[str, object]] = []

    parquet_files = sorted(PARQUET_DIR.glob("*.parquet"))
    if not parquet_files:
        print(f"没有找到 parquet 文件: {PARQUET_DIR}")
        return pd.DataFrame()

    for number, path in enumerate(parquet_files, start=1):
        code = normalize_code(path.stem)
        name = name_map.get(code, "")
        if not args.include_st and is_st_stock(name):
            continue

        try:
            df = load_price_frame(path)
            events = build_events(df, args.trend_threshold)
            current = build_current_event(df, args.trend_threshold)
            row = stock_result_row(
                code=code,
                name=name,
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
            print(f"读取失败: {path}, {type(exc).__name__}: {exc}")
            continue

        if row is not None:
            rows.append(row)

        if args.progress and number % 500 == 0:
            print(f"已扫描 {number}/{len(parquet_files)}，当前入选 {len(rows)} 只")

    if not rows:
        return pd.DataFrame()

    result = pd.DataFrame(rows)
    result["_stage_sort"] = result["current_stage"].map({
        "bottom_area": 0,
        "just_rebound": 1,
        "confirmed_rebound": 2,
    })
    result = result.sort_values(
        ["_stage_sort", "historical_valid_event_count", "current_low_to_latest_pct", "code"],
        ascending=[True, False, True, True],
    ).reset_index(drop=True)
    result = result.drop(columns=["_stage_sort"])
    return result


def explanation_frame(args: argparse.Namespace) -> pd.DataFrame:
    rows = [
        ("筛选目标", "查找当前从 7% 波段高点回撤约 30%，并处于底部附近、刚刚反弹或已确认反弹的股票。"),
        ("历史验证", f"历史上至少出现 {args.min_events} 次有效事件：高点到低点回撤 {args.drawdown_min * 100:.0f}%-{args.drawdown_max * 100:.0f}%，低点后最高涨幅 >= {args.full_rebound_min * 100:.0f}%。"),
        ("当前要求", f"当前高点到当前低点回撤 {args.drawdown_min * 100:.0f}%-{args.drawdown_max * 100:.0f}%，当前不强制涨到 {args.full_rebound_min * 100:.0f}%。"),
        ("当前价格涨幅限制", f"最新收盘价距离当前低点必须 <= {args.current_rebound_max * 100:.0f}%，避免当前价格已经涨太多。"),
        ("ST过滤", "默认排除名称中包含 ST 的股票；如需临时包含，可运行时加 --include-st。"),
        ("趋势规则", f"上涨或下跌超过 {args.trend_threshold * 100:.0f}% 才确认趋势切换，小于该幅度的震荡忽略。"),
        ("bottom_area", f"最新收盘距离当前低点 <= {args.bottom_area_max * 100:.0f}%，表示仍在底部附近。"),
        ("just_rebound", f"最新收盘距离当前低点 > {args.bottom_area_max * 100:.0f}%，但当前低点后最高涨幅 < {args.full_rebound_min * 100:.0f}%。"),
        ("confirmed_rebound", f"当前低点后最高涨幅 >= {args.full_rebound_min * 100:.0f}%，表示已经明显反弹。"),
        ("current_is_bottom_confirmed", "True 表示当前低点已经通过 7% 上涨确认；False 表示仍按最后高点后的运行中最低点判断。"),
        ("current_drawdown_pct", "(current_top_price - current_low_price) / current_top_price * 100。"),
        ("current_low_to_high_pct", "(current_rebound_high - current_low_price) / current_low_price * 100。"),
        ("current_low_to_latest_pct", "(latest_close - current_low_price) / current_low_price * 100。"),
        ("previous_events", "历史有效事件摘要：高点 -> 低点 dd=回撤幅度 -> 反弹高点 rb=反弹幅度。"),
    ]
    return pd.DataFrame(rows, columns=["项目", "说明"])


def write_excel(result: pd.DataFrame, args: argparse.Namespace) -> pd.DataFrame:
    excel = result.drop(columns=["_events"])
    with pd.ExcelWriter(args.output) as writer:
        excel.to_excel(writer, index=False, sheet_name="筛选结果")
        explanation_frame(args).to_excel(writer, index=False, sheet_name="说明")
    return excel


def format_float(value) -> str:
    if pd.isna(value):
        return ""
    return f"{float(value):.2f}"


def write_charts(result: pd.DataFrame, args: argparse.Namespace) -> list[Path]:
    if result.empty or args.charts <= 0:
        return []

    args.chart_dir.mkdir(parents=True, exist_ok=True)
    chart_paths: list[Path] = []

    for _, row in result.head(args.charts).iterrows():
        code = str(row["code"])
        df = load_price_frame(PARQUET_DIR / f"{code}.parquet")
        events = row["_events"]
        out_path = args.chart_dir / f"{code}_drawdown_rebound.svg"
        make_svg_chart(code, str(row["name"]), df, events, out_path)
        chart_paths.append(out_path)

    return chart_paths


def run_self_test() -> None:
    dates = pd.date_range("2026-01-01", periods=16, freq="D")
    df = pd.DataFrame({
        "date": dates,
        "high": [10, 11, 12, 14, 13, 11, 10, 9.8, 10.8, 12.2, 12.9, 12.0, 13.5, 15.0, 14.0, 16.0],
        "low": [9.8, 10.5, 11.5, 13.8, 12.8, 10.2, 9.7, 9.6, 10.0, 11.8, 12.5, 11.5, 13.0, 14.4, 13.7, 15.5],
        "close": [10, 10.8, 11.8, 14, 13, 10.5, 9.8, 9.7, 10.7, 12.0, 12.8, 11.7, 13.3, 14.8, 13.9, 15.8],
    })
    events = build_events(df, 0.07)
    assert events, "expected at least one event"
    first = events[0]
    assert first.drawdown_pct > 0.27, first.drawdown_pct
    assert first.rebound_pct > 0.20, first.rebound_pct
    current = build_current_event(df, 0.07)
    assert current is not None, "expected current event"
    print("self-test passed")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="筛选历史上出现过 30% 回撤后反弹 20%，当前又回撤到位且处于底部附近或刚反弹的股票。"
    )
    parser.add_argument("--trend-threshold", type=float, default=0.07, help="趋势反转阈值，默认 0.07")
    parser.add_argument("--drawdown-min", type=float, default=0.27, help="回撤下限，默认 0.27")
    parser.add_argument("--drawdown-max", type=float, default=0.33, help="回撤上限，默认 0.33")
    parser.add_argument("--bottom-area-max", "--start-rebound-min", dest="bottom_area_max", type=float, default=0.05, help="最新收盘距离当前低点不超过该比例时标记为底部附近，默认 0.05")
    parser.add_argument("--current-rebound-max", type=float, default=0.07, help="最新收盘距离当前低点的最大涨幅，默认 0.07")
    parser.add_argument("--full-rebound-min", type=float, default=0.20, help="历史有效事件的低点后最高涨幅下限，默认 0.20")
    parser.add_argument("--min-events", type=int, default=1, help="至少出现的历史有效事件次数，默认 1")
    parser.add_argument("--output", type=Path, default=OUT_FILE, help=f"输出 Excel，默认 {OUT_FILE}")
    parser.add_argument("--chart-dir", type=Path, default=CHART_DIR, help=f"SVG 图表目录，默认 {CHART_DIR}")
    parser.add_argument("--charts", type=int, default=5, help="为前 N 个结果生成 SVG 图，默认 5")
    parser.add_argument("--progress", action="store_true", help="扫描时打印进度")
    parser.add_argument("--include-st", action="store_true", help="包含名称中带 ST 的股票；默认排除")
    parser.add_argument("--self-test", action="store_true", help="运行内置小样本测试")
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    if args.self_test:
        run_self_test()
        return

    result = scan_stocks(args)
    if result.empty:
        print("没有筛选到符合条件的股票")
        return

    chart_paths = write_charts(result, args)

    excel = write_excel(result, args)

    print(excel.to_string(
        index=False,
        max_colwidth=80,
        formatters={
            "latest_close": format_float,
            "current_top_price": format_float,
            "current_top_confirm_price": format_float,
            "current_low_price": format_float,
            "current_low_confirm_price": format_float,
            "current_drawdown_pct": format_float,
            "current_rebound_high": format_float,
            "current_rebound_pct": format_float,
            "latest_close_rebound_pct": format_float,
            "current_low_to_high_pct": format_float,
            "current_low_to_latest_pct": format_float,
        },
    ))
    print()
    print(f"已导出: {args.output.resolve()}")
    print(f"共 {len(excel)} 只")
    if chart_paths:
        print("已生成图表:")
        for path in chart_paths:
            print(f"  {path.resolve()}")


if __name__ == "__main__":
    main()
