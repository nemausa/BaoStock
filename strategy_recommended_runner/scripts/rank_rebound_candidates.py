from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd

from filter_drawdown_rebound_history import (
    PARQUET_DIR,
    build_current_event,
    build_events,
    load_price_frame,
)


SCREENING_FILE = Path("drawdown_rebound_history_result.xlsx")
OUT_FILE = Path("tomorrow_rebound_probability_ranking.xlsx")


def normalize_code(code) -> str:
    code = str(code).strip()
    if "." in code:
        code = code.split(".")[-1]
    return code.zfill(6)


def read_screening(path: Path) -> pd.DataFrame:
    if not path.exists():
        raise FileNotFoundError(f"没有找到筛选结果: {path}")

    df = pd.read_excel(path, sheet_name="筛选结果", dtype={"code": str})
    if df.empty:
        raise RuntimeError(f"筛选结果为空: {path}")

    df["code"] = df["code"].apply(normalize_code)
    return df


def raw_price_frame(code: str) -> pd.DataFrame:
    path = PARQUET_DIR / f"{normalize_code(code)}.parquet"
    df = pd.read_parquet(path)
    df = df.copy()
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    return df.sort_values("date").reset_index(drop=True)


def safe_float(value, default: float = 0.0) -> float:
    try:
        if pd.isna(value):
            return default
        return float(value)
    except Exception:
        return default


def score_candidates(args: argparse.Namespace) -> pd.DataFrame:
    screening = read_screening(args.screening)
    rows: list[dict[str, object]] = []

    for _, row in screening.iterrows():
        code = normalize_code(row["code"])
        name = str(row["name"])

        df = load_price_frame(PARQUET_DIR / f"{code}.parquet")
        raw = raw_price_frame(code)
        events = build_events(df, args.trend_threshold)
        current = build_current_event(df, args.trend_threshold)
        if current is None:
            continue

        previous_same_drawdown = [
            event
            for event in events
            if event.top_date < current.top_date
            and args.drawdown_min <= event.drawdown_pct <= args.drawdown_max
        ]
        previous_success = [
            event
            for event in previous_same_drawdown
            if event.rebound_pct >= args.full_rebound_min
        ]
        sample_count = len(previous_same_drawdown)
        success_count = len(previous_success)
        success_rate = success_count / sample_count if sample_count else 0.0
        avg_rebound = sum(event.rebound_pct for event in previous_success) / success_count if success_count else 0.0
        max_rebound = max((event.rebound_pct for event in previous_success), default=0.0)

        latest = raw.iloc[-1]
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

        rows.append({
            "rank_score": rank_score,
            "probability_score": probability_score,
            "code": code,
            "name": name,
            "current_stage": stage,
            "latest_date": row["latest_date"],
            "latest_close": row["latest_close"],
            "current_top_date": str(row.get("current_top_date", "")),
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
            "history_success_dates": "; ".join(
                event.low_date.strftime("%Y-%m-%d") for event in previous_success
            ),
            "upside_to_20pct_target_pct": upside_to_20 * 100,
            "risk_back_to_low_pct": risk_to_low * 100,
            "reason": (
                f"历史{success_count}/{sample_count}次成功; "
                f"当前距低点{current_low_to_latest * 100:.2f}%; "
                f"最新日涨幅{latest_pct_chg:.2f}%; "
                f"状态{stage}; "
                f"{'底部已确认' if bottom_confirmed else '底部未确认'}"
            ),
        })

    if not rows:
        return pd.DataFrame()

    ranking = pd.DataFrame(rows).sort_values(
        ["rank_score", "probability_score"],
        ascending=False,
    ).reset_index(drop=True)
    ranking.insert(0, "rank", range(1, len(ranking) + 1))
    return ranking


def explanation_frame(args: argparse.Namespace) -> pd.DataFrame:
    rows = [
        ("用途", "按本地历史形态给出候选排序，不是确定性买入建议。"),
        ("硬条件", f"排除 ST；当前回撤 {args.drawdown_min * 100:.0f}%-{args.drawdown_max * 100:.0f}%；最新收盘距当前低点 <= {args.current_rebound_max * 100:.0f}%；历史至少一次同类回撤后反弹 >= {args.full_rebound_min * 100:.0f}%。"),
        ("rank_score", "综合排序分，越高代表历史胜率、当前低位、温和启动三者更好。"),
        ("probability_score", "概率倾向分，历史成功率占主要权重，当前状态占辅助权重。"),
        ("history_success_rate_pct", "历史同类回撤事件中，低点后最高涨幅达到 20% 的比例。"),
        ("current_low_to_latest_pct", "最新收盘价相对当前低点的涨幅。"),
        ("close_drawdown_pct", "(current_top_price - latest_close) / current_top_price * 100，按最新收盘价衡量当前回撤。"),
        ("风险提示", "A 股次日涨跌受市场、板块、公告、流动性影响很大；排名只适合做复盘和候选池。"),
    ]
    return pd.DataFrame(rows, columns=["项目", "说明"])


def write_ranking(ranking: pd.DataFrame, args: argparse.Namespace) -> None:
    with pd.ExcelWriter(args.output) as writer:
        ranking.to_excel(writer, index=False, sheet_name="概率排行")
        explanation_frame(args).to_excel(writer, index=False, sheet_name="说明")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="根据当前筛选结果生成反弹概率排行。")
    parser.add_argument("--screening", type=Path, default=SCREENING_FILE, help=f"筛选结果 Excel，默认 {SCREENING_FILE}")
    parser.add_argument("--output", type=Path, default=OUT_FILE, help=f"输出排行 Excel，默认 {OUT_FILE}")
    parser.add_argument("--trend-threshold", type=float, default=0.07, help="趋势反转阈值，默认 0.07")
    parser.add_argument("--drawdown-min", type=float, default=0.27, help="回撤下限，默认 0.27")
    parser.add_argument("--drawdown-max", type=float, default=0.33, help="回撤上限，默认 0.33")
    parser.add_argument("--current-rebound-max", type=float, default=0.07, help="最新收盘距离当前低点的最大涨幅，默认 0.07")
    parser.add_argument("--full-rebound-min", type=float, default=0.20, help="历史有效事件反弹下限，默认 0.20")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    ranking = score_candidates(args)
    if ranking.empty:
        print("没有可排序的候选股票")
        return

    write_ranking(ranking, args)
    print(ranking.head(20).to_string(index=False, max_colwidth=80))
    print()
    print(f"已导出: {args.output.resolve()}")
    print(f"共 {len(ranking)} 只")


if __name__ == "__main__":
    main()
