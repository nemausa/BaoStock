from __future__ import annotations

import argparse
import html
import re
from pathlib import Path

import pandas as pd

from backtest_key_level_trailing_stop import normalize_code
from export_key_level_50_breakout_candidate import KEY_LEVEL, PARQUET_DIR


DEFAULT_BASE = Path("strategy_recommended_runner") / "outputs" / "livermore_key_levels"
DEFAULT_PREFIX = "key_level_50_2026_01_returns"
DEFAULT_ALL_RETURNS_FILE = DEFAULT_BASE / f"{DEFAULT_PREFIX}_all_returns.csv"
DEFAULT_SINGLE_HOLDING_FILE = DEFAULT_BASE / f"{DEFAULT_PREFIX}_single_holding_trades.csv"
DEFAULT_ALL_OUT_DIR = DEFAULT_BASE / "charts" / "key_level_50_2026_01_all_returns"
DEFAULT_SINGLE_OUT_DIR = DEFAULT_BASE / "charts" / "key_level_50_2026_01_single_holding"
DEFAULT_NEXT_DAYS = 20


def safe_filename(value: object) -> str:
    text = str(value).strip()
    text = re.sub(r"[\\/:*?\"<>|\s]+", "_", text)
    return text.strip("_")


def scale(value: float, src_min: float, src_max: float, dst_min: float, dst_max: float) -> float:
    if src_max == src_min:
        return (dst_min + dst_max) / 2.0
    return dst_max - (value - src_min) / (src_max - src_min) * (dst_max - dst_min)


def svg_text(x: float, y: float, text: object, size: int = 12, fill: str = "#1f2937", anchor: str = "start") -> str:
    return (
        f'<text x="{x:.1f}" y="{y:.1f}" font-size="{size}" fill="{fill}" '
        f'font-family="Arial, Microsoft YaHei, sans-serif" text-anchor="{anchor}">'
        f"{html.escape(str(text))}</text>"
    )


def fmt_price(value: object) -> str:
    if pd.isna(value) or value == "":
        return "-"
    return f"{float(value):.2f}"


def fmt_pct(value: object) -> str:
    if pd.isna(value) or value == "":
        return "-"
    return f"{float(value):.2f}%"


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


def date_value(row: pd.Series, column: str) -> pd.Timestamp | None:
    value = row.get(column, "")
    if pd.isna(value) or value == "":
        return None
    parsed = pd.to_datetime(value, errors="coerce")
    return None if pd.isna(parsed) else pd.Timestamp(parsed)


def numeric_value(row: pd.Series, column: str) -> float | None:
    value = row.get(column, pd.NA)
    if pd.isna(value) or value == "":
        return None
    return float(value)


def chart_window(row: pd.Series, next_days: int) -> pd.DataFrame | None:
    code = normalize_code(row["股票代码"])
    signal_date = date_value(row, "信号日")
    if signal_date is None:
        return None

    df = load_price_frame(code)
    if df is None or df.empty:
        return None

    start_indices = df.index[df["date"] >= signal_date]
    if len(start_indices) == 0:
        return None

    start_idx = int(start_indices[0])
    window = df.iloc[start_idx: start_idx + next_days + 1].copy()
    if window.empty:
        return None

    window = window.reset_index(drop=True)
    window["day_no"] = range(len(window))
    return window


def render_chart(row: pd.Series, window: pd.DataFrame, mode: str) -> str:
    width = 1240
    height = 700
    left = 68
    right = 34
    top = 112
    price_bottom = 488
    volume_top = 535
    volume_bottom = 632
    plot_width = width - left - right
    plot_height = price_bottom - top
    n = len(window)
    step = plot_width / max(n, 1)
    candle_width = min(18.0, max(6.0, step * 0.55))

    buy_date = date_value(row, "买入日")
    sell_date = date_value(row, "卖出日")
    buy_price = numeric_value(row, "买入价")
    sell_price = numeric_value(row, "卖出价")
    target_price = numeric_value(row, "目标止盈价")
    return_pct = numeric_value(row, "收益率%")

    extra_prices = [KEY_LEVEL]
    for value in (buy_price, sell_price, target_price):
        if value is not None:
            extra_prices.append(value)

    price_min = min(float(window["low"].min()), min(extra_prices))
    price_max = max(float(window["high"].max()), max(extra_prices))
    pad = max((price_max - price_min) * 0.08, price_max * 0.01, 0.8)
    y_min = price_min - pad
    y_max = price_max + pad
    max_volume = max(float(window["volume"].fillna(0).max()) if "volume" in window.columns else 0.0, 1.0)

    code = normalize_code(row["股票代码"])
    name = row["股票名称"]
    rank = row.get("日内排名", "-")
    signal_date_text = row["信号日"]
    title = f"{signal_date_text} {code} {name}  50关键点突破K线"
    subtitle = (
        f"{mode} | 排名 {rank} | D0收盘 {fmt_price(row.get('D0收盘'))} | "
        f"D0涨幅 {fmt_pct(row.get('D0涨幅%'))} | 买入 {fmt_price(buy_price)} | "
        f"卖出 {fmt_price(sell_price)} | 收益 {fmt_pct(return_pct)} | {row.get('卖出原因', '')}"
    )

    parts: list[str] = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
        '<rect width="100%" height="100%" fill="#ffffff"/>',
        svg_text(left, 38, title, 21, "#111827"),
        svg_text(left, 66, subtitle, 13, "#4b5563"),
        f'<rect x="{left}" y="{top}" width="{plot_width}" height="{plot_height}" fill="#fbfbfb" stroke="#d1d5db"/>',
        f'<rect x="{left}" y="{volume_top}" width="{plot_width}" height="{volume_bottom - volume_top}" fill="#fbfbfb" stroke="#d1d5db"/>',
    ]

    for i in range(5):
        price = y_min + (y_max - y_min) * i / 4
        y = scale(price, y_min, y_max, top, price_bottom)
        parts.append(f'<line x1="{left}" y1="{y:.1f}" x2="{left + plot_width}" y2="{y:.1f}" stroke="#e5e7eb"/>')
        parts.append(svg_text(left - 8, y + 4, f"{price:.2f}", 11, "#6b7280", "end"))

    def x_for_date(date: pd.Timestamp | None) -> float | None:
        if date is None:
            return None
        matches = window.index[window["date"].eq(date)]
        if len(matches) == 0:
            return None
        idx = int(matches[0])
        return left + step * idx + step / 2.0

    buy_x = x_for_date(buy_date)
    sell_x = x_for_date(sell_date)
    if buy_x is not None and sell_x is not None and sell_x >= buy_x:
        parts.append(
            f'<rect x="{buy_x - step / 2:.1f}" y="{top}" width="{sell_x - buy_x + step:.1f}" '
            f'height="{plot_height}" fill="#fef3c7" opacity="0.35"/>'
        )

    y50 = scale(KEY_LEVEL, y_min, y_max, top, price_bottom)
    parts.append(
        f'<line x1="{left}" y1="{y50:.1f}" x2="{left + plot_width}" y2="{y50:.1f}" '
        'stroke="#2563eb" stroke-width="1.8" stroke-dasharray="7 5"/>'
    )
    parts.append(svg_text(left + plot_width - 4, y50 - 7, "50关键价位", 12, "#2563eb", "end"))

    if target_price is not None:
        y_target = scale(target_price, y_min, y_max, top, price_bottom)
        parts.append(
            f'<line x1="{left}" y1="{y_target:.1f}" x2="{left + plot_width}" y2="{y_target:.1f}" '
            'stroke="#9333ea" stroke-width="1.4" stroke-dasharray="5 5"/>'
        )
        parts.append(svg_text(left + plot_width - 4, y_target - 7, "15%止盈价", 12, "#9333ea", "end"))

    for row_price in window.itertuples(index=False):
        day = int(row_price.day_no)
        x = left + step * day + step / 2.0
        open_price = float(row_price.open)
        high_price = float(row_price.high)
        low_price = float(row_price.low)
        close_price = float(row_price.close)
        volume = float(row_price.volume) if hasattr(row_price, "volume") and pd.notna(row_price.volume) else 0.0
        color = "#dc2626" if close_price >= open_price else "#16a34a"

        y_open = scale(open_price, y_min, y_max, top, price_bottom)
        y_close = scale(close_price, y_min, y_max, top, price_bottom)
        y_high = scale(high_price, y_min, y_max, top, price_bottom)
        y_low = scale(low_price, y_min, y_max, top, price_bottom)
        body_top = min(y_open, y_close)
        body_height = max(abs(y_open - y_close), 1.5)
        parts.append(f'<line x1="{x:.1f}" y1="{y_high:.1f}" x2="{x:.1f}" y2="{y_low:.1f}" stroke="{color}" stroke-width="1.4"/>')
        parts.append(
            f'<rect x="{x - candle_width / 2:.1f}" y="{body_top:.1f}" width="{candle_width:.1f}" '
            f'height="{body_height:.1f}" fill="{color}" opacity="0.86"/>'
        )

        vol_height = volume / max_volume * (volume_bottom - volume_top)
        parts.append(
            f'<rect x="{x - candle_width / 2:.1f}" y="{volume_bottom - vol_height:.1f}" '
            f'width="{candle_width:.1f}" height="{vol_height:.1f}" fill="{color}" opacity="0.35"/>'
        )

        trade_date = pd.Timestamp(row_price.date)
        if day == 0:
            parts.append(f'<line x1="{x:.1f}" y1="{top}" x2="{x:.1f}" y2="{price_bottom}" stroke="#6b7280" stroke-dasharray="3 4"/>')
            parts.append(svg_text(x, top - 12, "D0", 12, "#374151", "middle"))
        if buy_date is not None and trade_date == buy_date and buy_price is not None:
            y_buy = scale(buy_price, y_min, y_max, top, price_bottom)
            parts.append(f'<circle cx="{x:.1f}" cy="{y_buy:.1f}" r="6" fill="#f59e0b" stroke="#92400e" stroke-width="1.5"/>')
            parts.append(svg_text(x, y_buy - 12, f"买入 {buy_price:.2f}", 12, "#92400e", "middle"))
        if sell_date is not None and trade_date == sell_date and sell_price is not None:
            y_sell = scale(sell_price, y_min, y_max, top, price_bottom)
            parts.append(f'<circle cx="{x:.1f}" cy="{y_sell:.1f}" r="6" fill="#111827" stroke="#111827" stroke-width="1.5"/>')
            parts.append(svg_text(x, y_sell - 12, f"卖出 {sell_price:.2f}", 12, "#111827", "middle"))

        if day % 2 == 0 or day == n - 1:
            parts.append(svg_text(x, volume_bottom + 23, pd.Timestamp(row_price.date).strftime("%m-%d"), 10, "#6b7280", "middle"))

    parts.append(svg_text(left, volume_top - 10, "成交量", 12, "#6b7280"))
    parts.append("</svg>")
    return "\n".join(parts)


def build_index(charts: list[dict[str, str]], out_dir: Path, title: str) -> str:
    cards = []
    for chart in charts:
        cards.append(
            '<section class="chart">'
            f'<h2>{html.escape(chart["title"])}</h2>'
            f'<img src="{html.escape(chart["file"])}" alt="{html.escape(chart["title"])}">'
            "</section>"
        )

    return """<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <title>""" + html.escape(title) + """</title>
  <style>
    body { margin: 0; padding: 24px; font-family: Arial, "Microsoft YaHei", sans-serif; background: #f3f4f6; color: #111827; }
    h1 { margin: 0 0 18px; font-size: 24px; }
    .chart { margin: 0 0 28px; padding: 16px; background: #fff; border: 1px solid #d1d5db; border-radius: 6px; }
    h2 { margin: 0 0 10px; font-size: 16px; font-weight: 600; }
    img { width: 100%; max-width: 1240px; height: auto; display: block; }
  </style>
</head>
<body>
  <h1>""" + html.escape(title) + """</h1>
""" + "\n".join(cards) + f"""
  <p>目录：{html.escape(str(out_dir.resolve()))}</p>
</body>
</html>
"""


def render_set(data: pd.DataFrame, out_dir: Path, title: str, mode: str, next_days: int) -> tuple[int, list[str]]:
    out_dir.mkdir(parents=True, exist_ok=True)
    for old_svg in out_dir.glob("*.svg"):
        old_svg.unlink()
    old_index = out_dir / "index.html"
    if old_index.exists():
        old_index.unlink()

    charts: list[dict[str, str]] = []
    skipped: list[str] = []

    data = data.copy()
    data["信号日_ts"] = pd.to_datetime(data["信号日"], errors="coerce")
    data = data.sort_values(["信号日_ts", "日内排名", "股票代码"])

    for _, row in data.iterrows():
        window = chart_window(row, next_days)
        if window is None:
            skipped.append(f"{row.get('信号日')} {row.get('股票代码')} {row.get('股票名称')}: 无法读取行情窗口")
            continue
        svg = render_chart(row, window, mode)
        filename = (
            f"{safe_filename(row.get('信号日'))}_rank{safe_filename(row.get('日内排名'))}_"
            f"{safe_filename(normalize_code(row.get('股票代码')))}_{safe_filename(row.get('股票名称'))}.svg"
        )
        chart_path = out_dir / filename
        chart_path.write_text(svg, encoding="utf-8")
        return_text = fmt_pct(row.get("收益率%"))
        cumulative = row.get("单持仓累计收益率%", "")
        if not pd.isna(cumulative) and cumulative != "":
            return_text = f"{return_text} / 累计 {fmt_pct(cumulative)}"
        charts.append(
            {
                "title": f"{row.get('信号日')} rank{row.get('日内排名')} {normalize_code(row.get('股票代码'))} {row.get('股票名称')} 收益 {return_text}",
                "file": filename,
            }
        )

    index_file = out_dir / "index.html"
    index_file.write_text(build_index(charts, out_dir, title), encoding="utf-8")
    return len(charts), skipped


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="导出2026年1月50关键点收益K线图。")
    parser.add_argument("--all-returns-file", type=Path, default=DEFAULT_ALL_RETURNS_FILE)
    parser.add_argument("--single-holding-file", type=Path, default=DEFAULT_SINGLE_HOLDING_FILE)
    parser.add_argument("--all-out-dir", type=Path, default=DEFAULT_ALL_OUT_DIR)
    parser.add_argument("--single-out-dir", type=Path, default=DEFAULT_SINGLE_OUT_DIR)
    parser.add_argument("--next-days", type=int, default=DEFAULT_NEXT_DAYS)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    all_returns = pd.read_csv(args.all_returns_file, dtype={"股票代码": str}, encoding="utf-8-sig")
    single_holding = pd.read_csv(args.single_holding_file, dtype={"股票代码": str}, encoding="utf-8-sig")

    all_count, all_skipped = render_set(
        all_returns,
        args.all_out_dir,
        "2026年1月 50关键点全部候选收益K线",
        "全部候选",
        args.next_days,
    )
    single_count, single_skipped = render_set(
        single_holding,
        args.single_out_dir,
        "2026年1月 50关键点单持仓收益K线",
        "单持仓",
        args.next_days,
    )

    print(f"全部候选输入: {args.all_returns_file.resolve()}")
    print(f"单持仓输入: {args.single_holding_file.resolve()}")
    print(f"全部候选输出: {args.all_out_dir.resolve()}")
    print(f"单持仓输出: {args.single_out_dir.resolve()}")
    print(f"全部候选SVG数量: {all_count}")
    print(f"单持仓SVG数量: {single_count}")
    if all_skipped or single_skipped:
        print("跳过:")
        for item in all_skipped + single_skipped:
            print(f"- {item}")
    print(f"全部候选HTML: {(args.all_out_dir / 'index.html').resolve()}")
    print(f"单持仓HTML: {(args.single_out_dir / 'index.html').resolve()}")


if __name__ == "__main__":
    main()
