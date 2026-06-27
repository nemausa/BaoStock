from __future__ import annotations

import argparse
from datetime import date
from pathlib import Path
import unicodedata

import pandas as pd

from backtest_key_level_trailing_stop import normalize_code


DEFAULT_EVENTS_FILE = (
    Path("strategy_recommended_runner") / "outputs" / "livermore_key_levels" / "events.csv"
)
DEFAULT_OUT_DIR = Path("strategy_recommended_runner") / "outputs" / "livermore_key_levels"
PARQUET_DIR = Path("a_stock_data") / "parquet"
KEY_LEVEL = 100.0


def load_events(path: Path) -> pd.DataFrame:
    events = pd.read_csv(path, dtype={"股票代码": str}, encoding="utf-8-sig", low_memory=False)
    events = events.copy()
    events["股票代码"] = events["股票代码"].apply(normalize_code)
    events["日期"] = pd.to_datetime(events["日期"], errors="coerce")

    for column in ["关键价位", "开盘价", "最高价", "最低价", "收盘价", "前收盘价"]:
        events[column] = pd.to_numeric(events[column], errors="coerce")

    events["D0涨幅%"] = (events["收盘价"] / events["前收盘价"] - 1.0) * 100.0
    events["突破幅度%"] = (events["收盘价"] / KEY_LEVEL - 1.0) * 100.0
    events["成交量放大_bool"] = events["成交量放大"].astype(str).str.lower().eq("true")
    return events


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


def add_next_open(candidates: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for _, row in candidates.iterrows():
        out = row.to_dict()
        df = load_price_frame(str(row["股票代码"]))
        out["次日交易日"] = ""
        out["次日开盘"] = pd.NA
        out["次日开盘相对D0收盘%"] = pd.NA
        out["次日开盘状态"] = "待确认"

        if df is not None and not df.empty:
            future_idx = df.index[df["date"] > pd.Timestamp(row["日期"])]
            if len(future_idx) > 0:
                buy_idx = int(future_idx[0])
                next_open = float(df.loc[buy_idx, "open"])
                d0_close = float(row["收盘价"])
                out["次日交易日"] = pd.Timestamp(df.loc[buy_idx, "date"]).strftime("%Y-%m-%d")
                out["次日开盘"] = next_open
                out["次日开盘相对D0收盘%"] = (next_open / d0_close - 1.0) * 100.0
                out["次日开盘状态"] = "可买" if next_open >= KEY_LEVEL else "不可买"

        rows.append(out)

    return pd.DataFrame(rows)


def select_candidates(events: pd.DataFrame, args: argparse.Namespace) -> pd.DataFrame:
    signal_date = pd.Timestamp(args.date)
    candidates = events[
        events["日期"].eq(signal_date)
        & events["事件类型"].eq("first_breakout_event")
        & events["关键价位"].eq(args.key_level)
        & events["趋势状态"].eq(args.trend_state)
        & events["收盘确认突破"].eq(True)
        & events["是否首次突破"].eq(True)
    ].copy()
    candidates = add_next_open(candidates)
    return candidates


def score_candidates(candidates: pd.DataFrame) -> pd.DataFrame:
    scored = candidates.copy()
    scored["评分"] = 0
    scored["评分说明"] = ""

    def add_score(mask: pd.Series, points: int, reason: str) -> None:
        scored.loc[mask, "评分"] += points
        scored.loc[mask, "评分说明"] = scored.loc[mask, "评分说明"].astype(str) + f"{reason};"

    add_score(scored["成交量放大_bool"], 3, "放量+3")
    add_score(scored["D0涨幅%"].ge(5.0) & scored["D0涨幅%"].lt(15.0), 3, "D0涨幅5-15%+3")
    add_score(scored["D0涨幅%"].ge(15.0) & scored["D0涨幅%"].lt(20.0), 1, "D0涨幅15-20%+1")
    add_score(scored["收盘价"].ge(100.0) & scored["收盘价"].le(108.0), 3, "收盘100-108+3")
    add_score(scored["收盘价"].gt(108.0) & scored["收盘价"].le(115.0), 1, "收盘108-115+1")
    add_score(scored["最低价"].ge(100.0), 2, "最低守住100+2")
    add_score((scored["最高价"] / scored["收盘价"]).le(1.05), 2, "上影小+2")

    has_next_open = scored["次日开盘"].notna()
    next_open = pd.to_numeric(scored["次日开盘"], errors="coerce")
    next_gap = pd.to_numeric(scored["次日开盘相对D0收盘%"], errors="coerce")
    add_score(has_next_open & next_open.ge(KEY_LEVEL), 5, "次日开盘>=100+5")
    add_score(has_next_open & next_gap.ge(-1.0) & next_gap.le(5.0), 3, "次日开盘温和+3")
    add_score(has_next_open & next_gap.gt(8.0), -3, "次日高开>8%-3")

    scored["是否可买"] = "待确认"
    scored.loc[has_next_open & next_open.ge(KEY_LEVEL), "是否可买"] = "是"
    scored.loc[has_next_open & next_open.lt(KEY_LEVEL), "是否可买"] = "否"

    scored["D0涨幅温和距离"] = (scored["D0涨幅%"] - 10.0).abs()
    scored["可买排序"] = scored["是否可买"].map({"是": 0, "待确认": 1, "否": 2}).fillna(1)
    scored = scored.sort_values(
        ["可买排序", "评分", "收盘价", "D0涨幅温和距离", "股票代码"],
        ascending=[True, False, False, True, True],
    ).reset_index(drop=True)
    scored.insert(0, "候选排名", range(1, len(scored) + 1))
    return scored


def output_columns(df: pd.DataFrame) -> list[str]:
    columns = [
        "候选排名",
        "股票代码",
        "股票名称",
        "评分",
        "是否可买",
        "次日交易日",
        "次日开盘",
        "次日开盘相对D0收盘%",
        "日期",
        "开盘价",
        "最高价",
        "最低价",
        "收盘价",
        "前收盘价",
        "D0涨幅%",
        "突破幅度%",
        "成交量放大",
        "趋势状态",
        "评分说明",
    ]
    return [column for column in columns if column in df.columns]


def display_width(value: object) -> int:
    text = str(value)
    width = 0
    for char in text:
        width += 2 if unicodedata.east_asian_width(char) in ("F", "W") else 1
    return width


def pad_display(value: object, width: int, align: str = "left") -> str:
    text = str(value)
    padding = max(width - display_width(text), 0)
    if align == "right":
        return " " * padding + text
    return text + " " * padding


def format_display_value(value: object, kind: str = "text") -> str:
    if pd.isna(value):
        return "-"
    if kind == "int":
        return str(int(value))
    if kind == "price":
        return f"{float(value):.2f}"
    if kind == "pct":
        return f"{float(value):.2f}"
    if kind == "bool_cn":
        return "是" if bool(value) else "否"
    text = str(value)
    return "-" if text in ("<NA>", "nan", "NaN", "None") else text


def print_ranked_preview(ranked: pd.DataFrame, top_n: int) -> None:
    if ranked.empty:
        return

    rows = []
    for _, row in ranked.head(top_n).iterrows():
        reason = format_display_value(row.get("评分说明", ""))
        reason = reason.rstrip(";")
        rows.append(
            {
                "排名": format_display_value(row.get("候选排名"), "int"),
                "代码": format_display_value(row.get("股票代码")),
                "名称": format_display_value(row.get("股票名称")),
                "评分": format_display_value(row.get("评分"), "int"),
                "状态": format_display_value(row.get("是否可买")),
                "次日开盘": format_display_value(row.get("次日开盘"), "price"),
                "D0收盘": format_display_value(row.get("收盘价"), "price"),
                "D0涨幅%": format_display_value(row.get("D0涨幅%"), "pct"),
                "突破幅度%": format_display_value(row.get("突破幅度%"), "pct"),
                "放量": "是" if bool(row.get("成交量放大_bool", False)) else "否",
                "说明": reason,
            }
        )

    headers = ["排名", "代码", "名称", "评分", "状态", "次日开盘", "D0收盘", "D0涨幅%", "突破幅度%", "放量", "说明"]
    numeric_headers = {"排名", "评分", "次日开盘", "D0收盘", "D0涨幅%", "突破幅度%"}
    widths = {
        header: max(display_width(header), *(display_width(row[header]) for row in rows))
        for header in headers
    }

    print("  ".join(pad_display(header, widths[header]) for header in headers))
    for row in rows:
        print(
            "  ".join(
                pad_display(row[header], widths[header], "right" if header in numeric_headers else "left")
                for header in headers
            )
        )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="导出利弗莫尔100关键点首次突破的单日候选。")
    parser.add_argument("--date", default=date.today().strftime("%Y-%m-%d"), help="信号日，格式 YYYY-MM-DD")
    parser.add_argument("--events-file", type=Path, default=DEFAULT_EVENTS_FILE)
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    parser.add_argument("--key-level", type=float, default=KEY_LEVEL)
    parser.add_argument("--trend-state", default="up")
    parser.add_argument("--top-n", type=int, default=20)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    events = load_events(args.events_file)
    data_dates = events["日期"].dropna()
    data_max = data_dates.max()
    signal_date = pd.Timestamp(args.date)

    if signal_date not in set(data_dates.unique()):
        print(f"信号日期: {args.date}")
        print(f"本地 events 最新日期: {data_max:%Y-%m-%d}")
        print("没有该日期的事件数据。请先更新行情并重建 livermore key level events。")
        return

    candidates = select_candidates(events, args)
    ranked = score_candidates(candidates) if not candidates.empty else candidates

    args.out_dir.mkdir(parents=True, exist_ok=True)
    out_csv = args.out_dir / f"key_level_100_breakout_candidate_{args.date}.csv"
    if ranked.empty:
        ranked.to_csv(out_csv, index=False, encoding="utf-8-sig")
    else:
        ranked[output_columns(ranked)].to_csv(out_csv, index=False, encoding="utf-8-sig", float_format="%.4f")

    print(f"信号日期: {args.date}")
    print("策略: 利弗莫尔100关键点首次突破；收盘确认突破；趋势=up")
    print("买入: 下一交易日开盘必须仍在100以上；若本地无次日行情，则开盘前人工确认")
    print("卖出: 跌破100卖出；15%止盈；从最高价回撤7%保护；最多持有10个交易日")
    print(f"候选数: {len(ranked)}")

    if ranked.empty:
        print("结论: 当日无符合规则股票，不买。")
    else:
        print_ranked_preview(ranked, args.top_n)
        first = ranked.iloc[0]
        print()
        if first["是否可买"] == "否":
            print("结论: 排名第一的次日开盘低于100，不买。")
        elif first["是否可买"] == "待确认":
            print(
                f"预选第一名: {first['股票代码']} {first['股票名称']}。"
                "次日开盘数据未确认，开盘必须 >=100 才买。"
            )
        else:
            print(
                f"最符合: {first['股票代码']} {first['股票名称']}，"
                f"次日开盘 {float(first['次日开盘']):.2f} >= 100，可作为买入候选。"
            )
    print(f"已保存: {out_csv.resolve()}")


if __name__ == "__main__":
    main()
