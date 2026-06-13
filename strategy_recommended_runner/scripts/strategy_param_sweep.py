"""
strategy_param_sweep.py
=======================
在固定选股条件（换手率>=5%, 回撤<=33%, rank1-only）下，全面扫描参数组合：
  - 止损   : 3%, 5%, 7%, 10%
  - 止盈   : 8%, 12%, 15%, 20%, 25%
  - 持仓   : 5, 10, 15, 20 天
  - 开盘过滤:
        "无过滤"         → None
        "低开>2%跳过"    → -2.0   (gap < -2%则跳过)
        "低开>1%跳过"    → -1.0
        "不低开才买"      →  0.0   (gap >= 0%才买)
        "高开>1%才买"    →  1.0   (gap >= 1%才买)

输出 Excel + 控制台 TOP20：
  - 按"2021-至今连续收益率"排序
  - 标注每年是否正收益（一致性判断）
  - 建议"年化>90%"的参数区间

用法：
  python strategy_recommended_runner/scripts/strategy_param_sweep.py
"""
from __future__ import annotations

import argparse
import copy
import sys
from datetime import date
from pathlib import Path

import pandas as pd


# ── 路径 ────────────────────────────────────────────────────────────────────
DAILY_REPLAY_DIR = Path("a_stock_data/verification_records/daily_replay")
PARQUET_DIR      = Path("a_stock_data/parquet")
OUTPUT_DIR       = Path("strategy_recommended_runner/outputs")

DEFAULT_PERIODS = [
    ("2021", "2021-01-01", "2021-12-31"),
    ("2022", "2022-01-01", "2022-12-31"),
    ("2023", "2023-01-01", "2023-12-31"),
    ("2024", "2024-01-01", "2024-12-31"),
    ("2025", "2025-01-01", "2025-12-31"),
    ("2026至今", "2026-01-01", date.today().strftime("%Y-%m-%d")),
    ("累计", "2021-01-01", date.today().strftime("%Y-%m-%d")),
]

YEAR_LABELS = ["2021", "2022", "2023", "2024", "2025", "2026至今"]  # 不含累计

# ── 扫描参数 ─────────────────────────────────────────────────────────────────
STOP_LOSS_LIST   = [0.03, 0.05, 0.07, 0.10]
TAKE_PROFIT_LIST = [0.08, 0.12, 0.15, 0.20, 0.25]
MAX_HOLD_LIST    = [5, 10, 15, 20]
GAP_FILTER_LIST: list[tuple[str, float | None]] = [
    ("无过滤",      None),
    ("低开>2%跳过", -2.0),
    ("低开>1%跳过", -1.0),
    ("不低开才买",   0.0),
    ("高开>1%才买",  1.0),
]

# 固定选股条件
MIN_TURN     = 5.0
MAX_DRAWDOWN = 33.0
INITIAL_CAPITAL = 50_000.0
LOT_SIZE        = 100
COOLDOWN_DAYS   = 10


# ── 工具函数（与 backtest_annual_returns.py 一致）───────────────────────────

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


def next_entry(price_cache: PriceCache, code: str, signal_date: str) -> dict | None:
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
    stop_loss: float,
    take_profit: float,
    max_hold_days: int,
) -> dict:
    df = price_cache.get(code)
    take_profit_price = entry_price * (1 + take_profit)
    stop_loss_price   = entry_price * (1 - stop_loss)
    last_index = min(len(df) - 1, entry_index + max_hold_days - 1)

    for idx in range(entry_index + 1, last_index + 1):
        row = df.loc[idx]
        holding_days = idx - entry_index + 1
        open_p = float(row["open"])
        # 跳空（低开穿破止损 / 高开穿破止盈）：按开盘价成交
        if open_p <= stop_loss_price:
            return {"exit_date": str(row["date_str"]), "exit_price": open_p, "exit_reason": "止损(低开)", "holding_days": holding_days}
        if open_p >= take_profit_price:
            return {"exit_date": str(row["date_str"]), "exit_price": open_p, "exit_reason": "止盈(高开)", "holding_days": holding_days}
        # 日内触碰
        if float(row["low"]) <= stop_loss_price:
            return {"exit_date": str(row["date_str"]), "exit_price": stop_loss_price, "exit_reason": "止损", "holding_days": holding_days}
        if float(row["high"]) >= take_profit_price:
            return {"exit_date": str(row["date_str"]), "exit_price": take_profit_price, "exit_reason": "止盈", "holding_days": holding_days}
        if holding_days >= max_hold_days:
            return {"exit_date": str(row["date_str"]), "exit_price": float(row["close"]), "exit_reason": "到期", "holding_days": holding_days}

    row = df.loc[last_index]
    return {"exit_date": str(row["date_str"]), "exit_price": float(row["close"]), "exit_reason": "到期",
            "holding_days": last_index - entry_index + 1}


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


def select_top1(daily: pd.DataFrame, min_turn: float | None = None, max_drawdown: float | None = None) -> pd.DataFrame:
    if daily.empty:
        return daily
    filtered = daily.copy()
    if min_turn is not None:
        filtered = filtered[numeric(filtered, "latest_turn") >= min_turn]
    if max_drawdown is not None:
        filtered = filtered[numeric(filtered, "current_drawdown_pct") <= max_drawdown]
    if filtered.empty:
        return filtered
    return filtered.sort_values(["signal_date", "rank"]).groupby("signal_date", group_keys=False).head(1).reset_index(drop=True)


def build_orders(candidates: pd.DataFrame, price_cache: PriceCache) -> pd.DataFrame:
    rows: list[dict] = []
    for _, row in candidates.iterrows():
        entry = next_entry(price_cache, str(row["code"]), str(row["signal_date"]))
        if entry is None:
            continue
        rows.append({**row.to_dict(), **entry})
    if not rows:
        return pd.DataFrame()
    return pd.DataFrame(rows).sort_values(["entry_date", "signal_date", "rank"]).reset_index(drop=True)


def simulate_one(
    orders: pd.DataFrame,
    price_cache: PriceCache,
    stop_loss: float,
    take_profit: float,
    max_hold_days: int,
    max_gap_down: float | None,
) -> tuple[pd.DataFrame, float]:
    """模拟一次交易，返回 (trades_df, final_asset)。"""
    if orders.empty:
        return pd.DataFrame(), INITIAL_CAPITAL

    cash = INITIAL_CAPITAL
    busy_until = ""
    cooldown: dict[str, str] = {}
    trades: list[dict] = []

    for _, order in orders.iterrows():
        entry_date = str(order["entry_date"])
        code = normalize_code(order["code"])

        if entry_date <= busy_until:
            continue
        if code in cooldown and entry_date <= cooldown[code]:
            continue
        if max_gap_down is not None and float(order.get("entry_gap_pct", 0.0)) < max_gap_down:
            continue

        entry_price = float(order["entry_open"])
        lot_cost = entry_price * LOT_SIZE
        quantity = int(cash // lot_cost) * LOT_SIZE if lot_cost > 0 else 0
        if quantity < LOT_SIZE:
            continue

        buy_amount = quantity * entry_price
        cash -= buy_amount

        exit_info = exit_trade(price_cache, code, int(order["entry_index"]),
                               entry_price, stop_loss, take_profit, max_hold_days)
        exit_price = float(exit_info["exit_price"])
        sell_amount = quantity * exit_price
        pnl = sell_amount - buy_amount
        cash += sell_amount

        trades.append({
            "signal_date": order.get("signal_date"),
            "code": code,
            "entry_date": entry_date,
            "entry_price": round(entry_price, 3),
            "entry_gap_pct": round(float(order.get("entry_gap_pct", 0.0)), 2),
            "exit_date": exit_info["exit_date"],
            "exit_price": round(exit_price, 3),
            "holding_days": exit_info["holding_days"],
            "exit_reason": exit_info["exit_reason"],
            "pnl": round(pnl, 2),
            "return_pct": round(pnl / buy_amount * 100 if buy_amount else 0.0, 2),
        })

        busy_until = str(exit_info["exit_date"])
        cooldown[code] = cooldown_until(price_cache, code, str(exit_info["exit_date"]), COOLDOWN_DAYS)

    return pd.DataFrame(trades), cash


def period_return(orders: pd.DataFrame, price_cache: PriceCache,
                  stop_loss: float, take_profit: float, max_hold_days: int,
                  max_gap_down: float | None) -> dict:
    trades, final_asset = simulate_one(orders, price_cache, stop_loss, take_profit, max_hold_days, max_gap_down)
    ret = (final_asset - INITIAL_CAPITAL) / INITIAL_CAPITAL * 100
    n = len(trades)
    wins = int((trades["pnl"] > 0).sum()) if n > 0 else 0
    stops = int(trades["exit_reason"].str.startswith("止损").sum()) if n > 0 else 0
    profits = int(trades["exit_reason"].str.startswith("止盈").sum()) if n > 0 else 0
    return {"ret": round(ret, 1), "trades": n, "wins": wins, "stops": stops, "profits": profits}


def main() -> None:
    parser = argparse.ArgumentParser(description="参数全扫描")
    parser.add_argument("--daily-dir", type=Path, default=DAILY_REPLAY_DIR)
    parser.add_argument("--parquet-dir", type=Path, default=PARQUET_DIR)
    parser.add_argument("--output", type=Path, default=OUTPUT_DIR / "param_sweep_results.xlsx")
    args = parser.parse_args()

    price_cache = PriceCache(args.parquet_dir)

    # 加载每个 period 的 ranking 数据
    print("加载快照数据…")
    daily_cache: dict[str, pd.DataFrame] = {}
    for label, start_date, end_date in DEFAULT_PERIODS:
        daily = load_daily_rankings(args.daily_dir, start_date, end_date)
        if daily.empty:
            print(f"  [{label}] 无数据，跳过")
            continue
        daily_cache[label] = daily
    if not daily_cache:
        print("没有数据，请先运行 run_daily_rebound_top3.sh 生成快照")
        sys.exit(1)

    # 为每个 period 构建 rank1 orders（换手率>=5%, 回撤<=33%）
    print(f"构建 orders（换手率>={MIN_TURN}%, 回撤<={MAX_DRAWDOWN}%, top1）…")
    orders_cache: dict[str, pd.DataFrame] = {}
    for label, daily in daily_cache.items():
        candidates = select_top1(daily, MIN_TURN, MAX_DRAWDOWN)
        orders = build_orders(candidates, price_cache)
        orders_cache[label] = orders
        print(f"  [{label}] {len(candidates)} 个信号, {len(orders)} 个 orders")

    # 统计一下开盘偏差分布（低开 vs 高开）
    all_orders = pd.concat([o for o in orders_cache.values() if not o.empty], ignore_index=True)
    if not all_orders.empty and "entry_gap_pct" in all_orders.columns:
        gaps = all_orders["entry_gap_pct"].dropna()
        print(f"\n开盘偏差分布（全部orders）：")
        print(f"  高开(>0%)  : {(gaps > 0).sum()} 次 ({(gaps > 0).mean()*100:.1f}%)")
        print(f"  平开(=0%)  : {(gaps == 0).sum()} 次")
        print(f"  低开(<0%)  : {(gaps < 0).sum()} 次 ({(gaps < 0).mean()*100:.1f}%)")
        print(f"  低开>2%    : {(gaps < -2).sum()} 次 ({(gaps < -2).mean()*100:.1f}%)")
        print(f"  低开>5%    : {(gaps < -5).sum()} 次 ({(gaps < -5).mean()*100:.1f}%)")
        print(f"  平均开盘偏差: {gaps.mean():.2f}%")

    # ── 全参数扫描 ────────────────────────────────────────────────────────────
    total_combos = len(STOP_LOSS_LIST) * len(TAKE_PROFIT_LIST) * len(MAX_HOLD_LIST) * len(GAP_FILTER_LIST)
    print(f"\n开始扫描 {total_combos} 种参数组合，请稍候…")
    results: list[dict] = []
    done = 0

    for sl in STOP_LOSS_LIST:
        for tp in TAKE_PROFIT_LIST:
            if tp <= sl:
                done += len(MAX_HOLD_LIST) * len(GAP_FILTER_LIST)
                continue
            for hd in MAX_HOLD_LIST:
                for gap_label, gap_val in GAP_FILTER_LIST:
                    row: dict = {
                        "止损%": int(sl * 100),
                        "止盈%": int(tp * 100),
                        "持仓天": hd,
                        "开盘过滤": gap_label,
                    }
                    cum_ret = None
                    all_positive = True
                    year_rets = []
                    for label, orders in orders_cache.items():
                        r = period_return(orders, price_cache, sl, tp, hd, gap_val)
                        row[label] = r["ret"]
                        if label == "累计":
                            cum_ret = r["ret"]
                            row["总交易数"] = r["trades"]
                            row["止损次数"] = r["stops"]
                            row["止盈次数"] = r["profits"]
                            row["止损率%"] = round(r["stops"] / r["trades"] * 100, 1) if r["trades"] else 0.0
                        elif label in YEAR_LABELS:
                            year_rets.append(r["ret"])
                            if r["ret"] < 0:
                                all_positive = False

                    row["累计收益%"] = cum_ret
                    row["每年正收益"] = "✓" if all_positive else "✗"
                    row["正收益年数"] = sum(1 for r in year_rets if r >= 0)
                    results.append(row)

                    done += 1
                    if done % 20 == 0:
                        pct = done / total_combos * 100
                        print(f"  进度: {done}/{total_combos} ({pct:.0f}%)…", end="\r")

    print(f"\n扫描完成，共 {len(results)} 种组合。")

    results_df = pd.DataFrame(results).sort_values("累计收益%", ascending=False).reset_index(drop=True)

    # 控制台输出 TOP20
    print("\n" + "=" * 80)
    print(f"TOP 20 参数组合（换手率>={MIN_TURN}%, 回撤<={MAX_DRAWDOWN}%, rank1-only）")
    print("=" * 80)
    year_cols = [l for l in YEAR_LABELS if l in results_df.columns]
    display_cols = ["止损%", "止盈%", "持仓天", "开盘过滤", "每年正收益", "正收益年数",
                    "总交易数", "止损率%"] + year_cols + ["累计收益%"]
    display = results_df[display_cols].head(20).copy()
    print(display.to_string(index=True, max_colwidth=15))
    print()

    # 分析：高开 vs 低开过滤哪个更好
    print("=" * 80)
    print("开盘过滤方式对比（固定止损7%, 止盈15%, 持仓10天）")
    print("=" * 80)
    sl7_tp15_hd10 = results_df[
        (results_df["止损%"] == 7) &
        (results_df["止盈%"] == 15) &
        (results_df["持仓天"] == 10)
    ][["开盘过滤", "每年正收益", "正收益年数", "总交易数", "止损率%"] + year_cols + ["累计收益%"]]
    if not sl7_tp15_hd10.empty:
        print(sl7_tp15_hd10.to_string(index=False))
    print()

    # 各止损级别最佳行
    print("=" * 80)
    print("各止损级别最优组合（每个止损取累计最高）")
    print("=" * 80)
    for sl in STOP_LOSS_LIST:
        best = results_df[results_df["止损%"] == int(sl * 100)].head(1)
        if not best.empty:
            r = best.iloc[0]
            print(f"  止损{int(sl*100)}%: 止盈{int(r['止盈%'])}%, 持仓{int(r['持仓天'])}天, "
                  f"{r['开盘过滤']}  →  累计{r['累计收益%']:+.1f}%, "
                  f"每年正收益:{r['每年正收益']}, 止损率:{r['止损率%']:.1f}%")
    print()

    # ── Excel 输出 ─────────────────────────────────────────────────────────
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with pd.ExcelWriter(args.output) as writer:
        # 全部结果
        out_cols = ["止损%", "止盈%", "持仓天", "开盘过滤", "每年正收益", "正收益年数",
                    "总交易数", "止损次数", "止盈次数", "止损率%"] + year_cols + ["累计收益%"]
        results_df[out_cols].to_excel(writer, index=True, sheet_name="全部组合排名")

        # TOP50
        results_df[out_cols].head(50).to_excel(writer, index=True, sheet_name="TOP50")

        # 每年都正收益的组合
        all_pos = results_df[results_df["每年正收益"] == "✓"]
        all_pos[out_cols].to_excel(writer, index=True, sheet_name="每年正收益组合")

        # 开盘过滤对比（各过滤方式的 TOP5）
        gap_best_rows = []
        for gap_label, _ in GAP_FILTER_LIST:
            top5 = results_df[results_df["开盘过滤"] == gap_label].head(5)
            gap_best_rows.append(top5)
        gap_best_df = pd.concat(gap_best_rows, ignore_index=True)
        gap_best_df[out_cols].to_excel(writer, index=True, sheet_name="开盘过滤方式对比")

        # 止损对比（各止损最优）
        sl_best = results_df.groupby("止损%").first().reset_index()
        sl_best[out_cols].to_excel(writer, index=False, sheet_name="各止损最优")

        # 参数说明
        pd.DataFrame([
            ("固定条件", f"换手率>={MIN_TURN}%, 回撤<={MAX_DRAWDOWN}%, rank1-only"),
            ("初始资金", f"{INITIAL_CAPITAL:,.0f}"),
            ("仓位", "全仓（整手买满）"),
            ("冷却期", f"同股卖出后 {COOLDOWN_DAYS} 交易日内不重买"),
            ("止损执行", "跳空低开穿破止损线：按开盘价成交（实际损失可能>止损%）"),
            ("止盈执行", "跳空高开穿破止盈线：按开盘价成交（实际收益可能>止盈%）"),
            ("开盘过滤说明", "'无过滤'=任何开盘均买; '不低开才买'=gap>=0%才买; '高开>1%才买'=gap>=1%才买"),
            ("数据范围", "2021-01-01 至今"),
        ], columns=["项目", "说明"]).to_excel(writer, index=False, sheet_name="说明")

    print(f"已保存: {args.output.resolve()}")

    # 分析开盘偏差与收益的关系
    if not all_orders.empty and "entry_gap_pct" in all_orders.columns:
        print("\n" + "=" * 80)
        print("补充分析：开盘偏差区间 vs 平均单笔收益（最优参数组合下）")
        print("=" * 80)

        # 用 TOP1 参数跑一次所有交易，看各开盘偏差区间的收益分布
        if not results_df.empty:
            best = results_df.iloc[0]
            best_sl = float(best["止损%"]) / 100
            best_tp = float(best["止盈%"]) / 100
            best_hd = int(best["持仓天"])
            best_gap = next(v for lbl, v in GAP_FILTER_LIST if lbl == best["开盘过滤"])

            all_trades_list = []
            for label, orders in orders_cache.items():
                if label == "累计" or orders.empty:
                    continue
                trades, _ = simulate_one(orders, price_cache, best_sl, best_tp, best_hd, best_gap)
                if not trades.empty:
                    trades["period"] = label
                    all_trades_list.append(trades)

            if all_trades_list:
                all_t = pd.concat(all_trades_list, ignore_index=True)
                bins = [-20, -5, -2, 0, 2, 5, 10, 30]
                labels_b = ["低开>5%", "低开2-5%", "低开0-2%", "高开0-2%", "高开2-5%", "高开5-10%", "高开>10%"]
                all_t["开盘区间"] = pd.cut(all_t["entry_gap_pct"], bins=bins, labels=labels_b, right=True)
                grp = all_t.groupby("开盘区间", observed=True)["return_pct"].agg(["count", "mean", lambda x: (x > 0).mean() * 100])
                grp.columns = ["交易数", "平均收益%", "胜率%"]
                grp = grp.round(2)
                print(f"最优参数: 止损{int(best_sl*100)}%, 止盈{int(best_tp*100)}%, 持仓{best_hd}天, {best['开盘过滤']}")
                print(grp.to_string())
                print()


if __name__ == "__main__":
    main()
