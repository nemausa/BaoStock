from __future__ import annotations

import argparse
import html
import re
from pathlib import Path

import pandas as pd


DEFAULT_BASE = Path("strategy_recommended_runner") / "outputs" / "livermore_key_levels"
DEFAULT_PREFIX = "key_level_100_breakout_2026_01_next20"
DEFAULT_PRICES_FILE = DEFAULT_BASE / f"{DEFAULT_PREFIX}_prices.csv"
DEFAULT_SUMMARY_FILE = DEFAULT_BASE / f"{DEFAULT_PREFIX}_summary.csv"
DEFAULT_OUT_DIR = DEFAULT_BASE / "charts" / DEFAULT_PREFIX


def safe_filename(value: str) -> str:
    value = str(value).strip()
    value = re.sub(r"[\\/:*?\"<>|\\s]+", "_", value)
    return value.strip("_")


def scale(value: float, src_min: float, src_max: float, dst_min: float, dst_max: float) -> float:
    if src_max == src_min:
        return (dst_min + dst_max) / 2.0
    return dst_max - (value - src_min) / (src_max - src_min) * (dst_max - dst_min)


def format_pct(value: object) -> str:
    if pd.isna(value):
        return ""
    return f"{float(value):.2f}%"


def svg_text(x: float, y: float, text: object, size: int = 12, fill: str = "#1f2937", anchor: str = "start") -> str:
    return (
        f'<text x="{x:.1f}" y="{y:.1f}" font-size="{size}" fill="{fill}" '
        f'font-family="Arial, Microsoft YaHei, sans-serif" text-anchor="{anchor}">'
        f"{html.escape(str(text))}</text>"
    )


def render_chart(group: pd.DataFrame, summary_row: pd.Series | None) -> str:
    group = group.sort_values("交易日序号").reset_index(drop=True)
    width = 1180
    height = 620
    left = 64
    right = 28
    top = 86
    price_bottom = 430
    volume_top = 470
    volume_bottom = 560
    plot_width = width - left - right
    plot_height = price_bottom - top
    n = len(group)
    step = plot_width / max(n, 1)
    candle_width = min(18.0, max(6.0, step * 0.55))

    price_min = float(group["最低价"].min())
    price_max = float(group["最高价"].max())
    pad = max((price_max - price_min) * 0.08, price_max * 0.01, 1.0)
    y_min = price_min - pad
    y_max = price_max + pad
    if y_min > 100.0:
        y_min = 100.0 - pad
    if y_max < 100.0:
        y_max = 100.0 + pad

    max_volume = float(group["成交量"].fillna(0).max())
    max_volume = max(max_volume, 1.0)

    first = group.iloc[0]
    signal_date = first["信号日"]
    code = first["股票代码"]
    name = first["股票名称"]
    title = f"{signal_date} {code} {name}  100关键点突破后20交易日K线"
    if summary_row is not None:
        subtitle = (
            f"D0涨幅 {format_pct(summary_row['D0涨幅%'])} | 放量 {summary_row['是否放量']} | "
            f"次日开盘 {float(summary_row['次日开盘']):.2f} | "
            f"20日收益 {format_pct(summary_row['20日收益%'])} | "
            f"首次跌破100 {summary_row['首次跌破100日期'] if pd.notna(summary_row['首次跌破100日期']) else '-'}"
        )
    else:
        subtitle = f"D0涨幅 {format_pct(first['D0涨幅%'])} | 放量 {first['是否放量']}"

    parts: list[str] = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
        '<rect width="100%" height="100%" fill="#ffffff"/>',
        svg_text(left, 34, title, 20, "#111827"),
        svg_text(left, 60, subtitle, 13, "#4b5563"),
        f'<rect x="{left}" y="{top}" width="{plot_width}" height="{plot_height}" fill="#fafafa" stroke="#d1d5db"/>',
        f'<rect x="{left}" y="{volume_top}" width="{plot_width}" height="{volume_bottom - volume_top}" fill="#fafafa" stroke="#d1d5db"/>',
    ]

    for i in range(5):
        price = y_min + (y_max - y_min) * i / 4
        y = scale(price, y_min, y_max, top, price_bottom)
        parts.append(f'<line x1="{left}" y1="{y:.1f}" x2="{left + plot_width}" y2="{y:.1f}" stroke="#e5e7eb"/>')
        parts.append(svg_text(left - 8, y + 4, f"{price:.2f}", 11, "#6b7280", "end"))

    if y_min <= 100.0 <= y_max:
        y100 = scale(100.0, y_min, y_max, top, price_bottom)
        parts.append(
            f'<line x1="{left}" y1="{y100:.1f}" x2="{left + plot_width}" y2="{y100:.1f}" '
            'stroke="#2563eb" stroke-width="1.5" stroke-dasharray="6 4"/>'
        )
        parts.append(svg_text(left + plot_width - 4, y100 - 6, "100关键价位", 12, "#2563eb", "end"))

    for row in group.itertuples(index=False):
        day = int(row.交易日序号)
        x = left + step * day + step / 2.0
        open_price = float(row.开盘价)
        high_price = float(row.最高价)
        low_price = float(row.最低价)
        close_price = float(row.收盘价)
        volume = float(row.成交量) if pd.notna(row.成交量) else 0.0
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
            f'height="{body_height:.1f}" fill="{color}" opacity="0.85"/>'
        )

        vol_height = volume / max_volume * (volume_bottom - volume_top)
        parts.append(
            f'<rect x="{x - candle_width / 2:.1f}" y="{volume_bottom - vol_height:.1f}" '
            f'width="{candle_width:.1f}" height="{vol_height:.1f}" fill="{color}" opacity="0.35"/>'
        )

        if day in (0, 1):
            marker = "D0" if day == 0 else "买入"
            parts.append(f'<line x1="{x:.1f}" y1="{top}" x2="{x:.1f}" y2="{price_bottom}" stroke="#9ca3af" stroke-dasharray="3 4"/>')
            parts.append(svg_text(x, top - 10, marker, 12, "#374151", "middle"))

        if day % 2 == 0 or day == n - 1:
            label = str(row.交易日期)[5:]
            parts.append(svg_text(x, volume_bottom + 22, label, 10, "#6b7280", "middle"))

    parts.append(svg_text(left, volume_top - 10, "成交量", 12, "#6b7280"))
    parts.append("</svg>")
    return "\n".join(parts)


def build_index(charts: list[dict[str, str]], out_dir: Path) -> str:
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
  <title>100关键点突破后20交易日K线</title>
  <style>
    body { margin: 0; padding: 24px; font-family: Arial, "Microsoft YaHei", sans-serif; background: #f3f4f6; color: #111827; }
    h1 { margin: 0 0 18px; font-size: 24px; }
    .chart { margin: 0 0 28px; padding: 16px; background: #fff; border: 1px solid #d1d5db; border-radius: 6px; }
    h2 { margin: 0 0 10px; font-size: 16px; font-weight: 600; }
    img { width: 100%; max-width: 1180px; height: auto; display: block; }
  </style>
</head>
<body>
  <h1>2026年1月 利弗莫尔100关键点突破后20交易日K线</h1>
""" + "\n".join(cards) + f"""
  <p>目录：{html.escape(str(out_dir.resolve()))}</p>
</body>
</html>
"""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="把100关键点突破后20交易日价格CSV导出为K线SVG图。")
    parser.add_argument("--prices-file", type=Path, default=DEFAULT_PRICES_FILE)
    parser.add_argument("--summary-file", type=Path, default=DEFAULT_SUMMARY_FILE)
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    prices = pd.read_csv(args.prices_file, dtype={"股票代码": str}, encoding="utf-8-sig")
    summary = pd.read_csv(args.summary_file, dtype={"股票代码": str}, encoding="utf-8-sig")

    args.out_dir.mkdir(parents=True, exist_ok=True)
    summary_by_key = {
        (row["信号日"], row["股票代码"]): row
        for _, row in summary.iterrows()
    }

    charts: list[dict[str, str]] = []
    group_columns = ["信号日", "股票代码", "股票名称"]
    for (signal_date, code, name), group in prices.groupby(group_columns, sort=True):
        key = (signal_date, code)
        summary_row = summary_by_key.get(key)
        svg = render_chart(group, summary_row)
        filename = f"{safe_filename(signal_date)}_{safe_filename(code)}_{safe_filename(name)}.svg"
        chart_path = args.out_dir / filename
        chart_path.write_text(svg, encoding="utf-8")
        charts.append({"title": f"{signal_date} {code} {name}", "file": filename})

    index_html = build_index(charts, args.out_dir)
    index_file = args.out_dir / "index.html"
    index_file.write_text(index_html, encoding="utf-8")

    print(f"输入明细: {args.prices_file.resolve()}")
    print(f"输入摘要: {args.summary_file.resolve()}")
    print(f"输出目录: {args.out_dir.resolve()}")
    print(f"SVG数量: {len(charts)}")
    print(f"索引HTML: {index_file.resolve()}")


if __name__ == "__main__":
    main()
