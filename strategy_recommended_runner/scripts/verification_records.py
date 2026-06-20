from __future__ import annotations

import argparse
import concurrent.futures
import json
import multiprocessing
import os
from datetime import datetime
from pathlib import Path

import pandas as pd


BASE_DIR = Path("a_stock_data")
PARQUET_DIR = BASE_DIR / "parquet"
RECORD_DIR = BASE_DIR / "verification_records"

SCREENING_FILE = Path("drawdown_rebound_history_result.xlsx")
RANKING_FILE = Path("tomorrow_rebound_probability_ranking.xlsx")
SUMMARY_FILE = RECORD_DIR / "verification_summary.xlsx"
DAILY_REPLAY_DIR = RECORD_DIR / "daily_replay"
MONTHLY_ANALYSIS_DIR = RECORD_DIR / "monthly_analysis"
PORTFOLIO_ANALYSIS_DIR = RECORD_DIR / "portfolio_analysis"
PORTFOLIO_OPTIMIZATION_DIR = PORTFOLIO_ANALYSIS_DIR / "optimization"
DEFAULT_HIT_THRESHOLDS = [3.0, 5.0, 10.0, 20.0]
RANK_SCORE_SCAN_THRESHOLDS = [80.0, 85.0, 88.0, 90.0, 92.0, 94.0, 96.0]
PROBABILITY_SCORE_SCAN_THRESHOLDS = [70.0, 75.0, 78.0, 80.0, 82.0, 84.0]
OPTIMIZE_TAKE_PROFITS = [0.02, 0.03, 0.04, 0.05]
OPTIMIZE_STOP_LOSSES = [0.03, 0.05, 0.06, 0.08]
OPTIMIZE_MAX_HOLD_DAYS = [5, 10, 15]
_BACKFILL_STOCKS = None
_BACKFILL_ARGS = None


def normalize_code(code) -> str:
    code = str(code).strip()
    if "." in code:
        code = code.split(".")[-1]
    return code.zfill(6)


def safe_float(value) -> float:
    converted = pd.to_numeric(pd.Series([value]), errors="coerce").iloc[0]
    if pd.isna(converted):
        return float("nan")
    return float(converted)


def read_sheet(path: Path, sheet_name: str) -> pd.DataFrame:
    if not path.exists():
        raise FileNotFoundError(f"没有找到文件: {path}")
    return pd.read_excel(path, sheet_name=sheet_name, dtype={"code": str})


def load_price_frame(code: str) -> pd.DataFrame:
    path = PARQUET_DIR / f"{normalize_code(code)}.parquet"
    if not path.exists():
        raise FileNotFoundError(f"没有找到行情文件: {path}")

    df = pd.read_parquet(path, columns=["date", "open", "high", "low", "close"])
    df = df.copy()
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    for column in ["open", "high", "low", "close"]:
        df[column] = pd.to_numeric(df[column], errors="coerce")

    df = df.dropna(subset=["date", "high", "low", "close"])
    return df.sort_values("date").reset_index(drop=True)


def snapshot_name(record_date: str, suffix: str | None) -> str:
    if suffix:
        safe_suffix = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in suffix)
        return f"{record_date}_{safe_suffix}"
    return record_date


def save_snapshot(args: argparse.Namespace) -> Path:
    ranking = read_sheet(args.ranking, "概率排行")
    ranking["code"] = ranking["code"].apply(normalize_code)

    screening = read_sheet(args.screening, "筛选结果")
    screening["code"] = screening["code"].apply(normalize_code)

    if ranking.empty:
        raise RuntimeError("概率排行为空，无法保存记录")

    record_date = str(ranking["latest_date"].iloc[0])
    record_name = snapshot_name(record_date, args.suffix)
    out_dir = args.record_dir / record_name
    out_dir.mkdir(parents=True, exist_ok=True)

    ranking_out = ranking.copy()
    screening_out = screening.copy()
    ranking_out.to_csv(out_dir / "ranking_snapshot.csv", index=False, encoding="utf-8-sig")
    screening_out.to_csv(out_dir / "screening_snapshot.csv", index=False, encoding="utf-8-sig")

    with pd.ExcelWriter(out_dir / "snapshot.xlsx") as writer:
        ranking_out.to_excel(writer, index=False, sheet_name="概率排行")
        screening_out.to_excel(writer, index=False, sheet_name="筛选结果")
        pd.DataFrame([
            ("record_date", record_date),
            ("saved_at", datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
            ("ranking_file", str(args.ranking)),
            ("screening_file", str(args.screening)),
            ("note", "用于后续验证候选股票记录日至验证日的收益、最高涨幅和最大回撤。"),
        ], columns=["项目", "说明"]).to_excel(writer, index=False, sheet_name="说明")

    manifest = {
        "record_date": record_date,
        "saved_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "ranking_file": str(args.ranking),
        "screening_file": str(args.screening),
        "ranking_rows": int(len(ranking_out)),
        "screening_rows": int(len(screening_out)),
    }
    (out_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    print(f"已保存记录: {out_dir.resolve()}")
    print(f"记录日期: {record_date}")
    print(f"股票数量: {len(ranking_out)}")
    return out_dir


def latest_snapshot(record_dir: Path) -> Path:
    if not record_dir.exists():
        raise FileNotFoundError(f"没有找到记录目录: {record_dir}")

    snapshots = [path for path in record_dir.iterdir() if path.is_dir() and (path / "ranking_snapshot.csv").exists()]
    if not snapshots:
        raise FileNotFoundError(f"没有任何记录快照: {record_dir}")

    return sorted(snapshots)[-1]


def snapshot_dirs(record_dir: Path) -> list[Path]:
    if not record_dir.exists():
        return []

    return sorted(path for path in record_dir.iterdir() if path.is_dir() and (path / "ranking_snapshot.csv").exists())


def daily_replay_snapshot_dir(record_dir: Path, record_date: str, suffix: str | None) -> Path:
    year, month = record_date[:4], record_date[5:7]
    return record_dir / "daily_replay" / year / month / snapshot_name(record_date, suffix)


def save_replay_snapshot(
    ranking: pd.DataFrame,
    replay_date: pd.Timestamp,
    args: argparse.Namespace,
) -> Path:
    record_date = replay_date.strftime("%Y-%m-%d")
    out_dir = daily_replay_snapshot_dir(args.record_dir, record_date, args.suffix)
    out_dir.mkdir(parents=True, exist_ok=True)

    ranking_out = ranking.copy()
    ranking_out["code"] = ranking_out["code"].apply(normalize_code)
    ranking_out.to_csv(out_dir / "ranking_snapshot.csv", index=False, encoding="utf-8-sig")

    screening_columns = [
        "code",
        "name",
        "latest_date",
        "latest_close",
        "current_stage",
        "current_top_price",
        "current_low_date",
        "current_low_price",
        "current_low_to_latest_pct",
        "current_low_to_high_pct",
        "current_drawdown_pct",
        "close_drawdown_pct",
        "current_is_bottom_confirmed",
        "historical_valid_event_count",
    ]
    screening_columns = [column for column in screening_columns if column in ranking_out.columns]
    screening_out = ranking_out[screening_columns].copy()
    screening_out.to_csv(out_dir / "screening_snapshot.csv", index=False, encoding="utf-8-sig")

    if not getattr(args, "no_excel_snapshot", False):
        with pd.ExcelWriter(out_dir / "snapshot.xlsx") as writer:
            ranking_out.to_excel(writer, index=False, sheet_name="概率排行")
            screening_out.to_excel(writer, index=False, sheet_name="筛选结果")
            pd.DataFrame([
                ("record_date", record_date),
                ("saved_at", datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
                ("source", "daily replay"),
                ("note", "回放每日模型结果，用于月度 Top3 验证；后续验证不再重新查找当日候选。"),
            ], columns=["项目", "说明"]).to_excel(writer, index=False, sheet_name="说明")

    manifest = {
        "record_date": record_date,
        "saved_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "source": "daily_replay",
        "ranking_rows": int(len(ranking_out)),
        "screening_rows": int(len(screening_out)),
        "trend_threshold": float(args.trend_threshold),
        "drawdown_min": float(args.drawdown_min),
        "drawdown_max": float(args.drawdown_max),
        "current_rebound_max": float(args.current_rebound_max),
        "full_rebound_min": float(args.full_rebound_min),
        "min_events": int(args.min_events),
        "include_st": bool(args.include_st),
    }
    (out_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    return out_dir


def default_backfill_workers() -> int:
    return max(1, (os.cpu_count() or 1) - 1)


def replay_snapshot_exists(args: argparse.Namespace, replay_date: pd.Timestamp) -> bool:
    record_date = replay_date.strftime("%Y-%m-%d")
    out_dir = daily_replay_snapshot_dir(args.record_dir, record_date, args.suffix)
    return (out_dir / "ranking_snapshot.csv").exists()


def init_backfill_worker(stocks, args: argparse.Namespace) -> None:
    global _BACKFILL_STOCKS, _BACKFILL_ARGS
    _BACKFILL_STOCKS = stocks
    _BACKFILL_ARGS = args


def run_backfill_date(replay_date: pd.Timestamp) -> dict[str, object]:
    from backtest_rebound_top3 import replay_day

    if _BACKFILL_STOCKS is None or _BACKFILL_ARGS is None:
        raise RuntimeError("backfill worker 未初始化")

    replay_date = pd.Timestamp(replay_date)
    args = _BACKFILL_ARGS
    if getattr(args, "skip_existing", False) and replay_snapshot_exists(args, replay_date):
        return {
            "date": replay_date,
            "status": "skipped_existing",
            "rows": 0,
            "out_dir": None,
        }

    ranking = replay_day(_BACKFILL_STOCKS, replay_date, args)
    if ranking.empty:
        return {
            "date": replay_date,
            "status": "empty",
            "rows": 0,
            "out_dir": None,
        }

    out_dir = save_replay_snapshot(ranking, replay_date, args)
    return {
        "date": replay_date,
        "status": "saved",
        "rows": int(len(ranking)),
        "out_dir": out_dir,
    }


def print_backfill_progress(result: dict[str, object], completed: int, total: int, args: argparse.Namespace) -> None:
    if not args.progress:
        return

    replay_date = pd.Timestamp(result["date"])
    status = result["status"]
    if status == "saved":
        rows = int(result["rows"])
        top_count = min(int(args.top_n), rows)
        print(
            f"已保存 {completed}/{total} {replay_date:%Y-%m-%d}: "
            f"候选 {rows} 只，Top{top_count} 可验证 -> {result['out_dir']}",
            flush=True,
        )
    elif status == "skipped_existing":
        print(f"跳过 {completed}/{total} {replay_date:%Y-%m-%d}: 快照已存在", flush=True)
    else:
        print(f"跳过 {completed}/{total} {replay_date:%Y-%m-%d}: 没有候选", flush=True)


def backfill_daily_snapshots(args: argparse.Namespace) -> list[Path]:
    from backtest_rebound_top3 import load_stock_data, replay_dates

    stocks = load_stock_data(args)
    if not stocks:
        raise RuntimeError("没有可用股票数据")

    dates = replay_dates(stocks, args)
    if not dates:
        raise RuntimeError(f"{args.start_date} 到 {args.end_date} 没有交易日数据")

    workers = args.workers if args.workers is not None else default_backfill_workers()
    workers = max(1, int(workers))
    if args.progress:
        excel_mode = "跳过每日 Excel" if args.no_excel_snapshot else "生成每日 Excel"
        print(
            f"开始回放 {len(dates)} 个交易日，worker={workers}，"
            f"{excel_mode}，股票数据已加载在内存中",
            flush=True,
        )

    results: list[dict[str, object]] = []
    if workers == 1:
        init_backfill_worker(stocks, args)
        for number, replay_date in enumerate(dates, start=1):
            result = run_backfill_date(replay_date)
            results.append(result)
            print_backfill_progress(result, number, len(dates), args)
    else:
        context = multiprocessing.get_context("fork")
        with concurrent.futures.ProcessPoolExecutor(
            max_workers=workers,
            mp_context=context,
            initializer=init_backfill_worker,
            initargs=(stocks, args),
        ) as executor:
            future_to_date = {
                executor.submit(run_backfill_date, replay_date): replay_date
                for replay_date in dates
            }
            completed = 0
            for future in concurrent.futures.as_completed(future_to_date):
                completed += 1
                replay_date = future_to_date[future]
                try:
                    result = future.result()
                except Exception as exc:
                    result = {
                        "date": replay_date,
                        "status": "error",
                        "rows": 0,
                        "out_dir": None,
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                results.append(result)
                print_backfill_progress(result, completed, len(dates), args)

    errors = [result for result in results if result["status"] == "error"]
    if errors:
        preview = "\n".join(
            f"{pd.Timestamp(result['date']):%Y-%m-%d}: {result['error']}"
            for result in sorted(errors, key=lambda item: item["date"])[:10]
        )
        raise RuntimeError(f"每日回放失败 {len(errors)} 个日期:\n{preview}")

    outputs = [
        result["out_dir"]
        for result in sorted(results, key=lambda item: item["date"])
        if result["status"] == "saved" and result["out_dir"] is not None
    ]

    print(f"已保存每日回放快照 {len(outputs)} 个")
    return outputs


def numeric_series(df: pd.DataFrame, column: str) -> pd.Series:
    if column not in df.columns:
        return pd.Series(dtype=float)
    return pd.to_numeric(df[column], errors="coerce")


def with_close_drawdown_pct(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty or "close_drawdown_pct" in df.columns:
        return df
    if "latest_close" not in df.columns:
        return df

    latest_close = numeric_series(df, "latest_close")
    if "current_top_price" in df.columns:
        current_top = numeric_series(df, "current_top_price")
    elif "current_low_price" in df.columns and "current_drawdown_pct" in df.columns:
        current_low = numeric_series(df, "current_low_price")
        current_drawdown = numeric_series(df, "current_drawdown_pct") / 100
        current_top = current_low / (1 - current_drawdown)
    else:
        return df

    out = df.copy()
    out["close_drawdown_pct"] = (current_top - latest_close) / current_top * 100
    return out


def build_summary_row(snapshot_dir: Path, verification: pd.DataFrame, record_date: pd.Timestamp) -> dict[str, object]:
    close_return = numeric_series(verification, "close_return_pct")
    max_gain = numeric_series(verification, "max_gain_pct")
    max_drawdown = numeric_series(verification, "max_drawdown_pct")

    row: dict[str, object] = {
        "snapshot": snapshot_dir.name,
        "snapshot_path": str(snapshot_dir),
        "record_date": record_date.strftime("%Y-%m-%d"),
        "verified_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "rows": int(len(verification)),
        "avg_close_return_pct": close_return.mean(),
        "median_close_return_pct": close_return.median(),
        "avg_max_gain_pct": max_gain.mean(),
        "median_max_gain_pct": max_gain.median(),
        "avg_max_drawdown_pct": max_drawdown.mean(),
        "positive_close_count": int((close_return > 0).sum()),
        "positive_close_rate_pct": (close_return > 0).mean() * 100 if len(close_return) else 0,
        "max_gain_ge_5_count": int((max_gain >= 5).sum()),
        "max_gain_ge_5_rate_pct": (max_gain >= 5).mean() * 100 if len(max_gain) else 0,
    }

    for threshold in DEFAULT_HIT_THRESHOLDS:
        column = f"hit_{int(threshold)}pct"
        if column in verification.columns:
            hits = verification[column].fillna(False).astype(bool)
            row[f"{column}_count"] = int(hits.sum())
            row[f"{column}_rate_pct"] = hits.mean() * 100 if len(hits) else 0

    return row


def verify_snapshot_path(snapshot_dir: Path, out_prefix: str = "verification") -> Path:
    ranking_path = snapshot_dir / "ranking_snapshot.csv"
    if not ranking_path.exists():
        raise FileNotFoundError(f"没有找到快照排行文件: {ranking_path}")

    ranking = pd.read_csv(ranking_path, dtype={"code": str})
    ranking["code"] = ranking["code"].apply(normalize_code)
    record_date = pd.to_datetime(ranking["latest_date"].iloc[0])

    rows = []
    for _, row in ranking.iterrows():
        code = normalize_code(row["code"])
        try:
            df = load_price_frame(code)
        except Exception as exc:
            rows.append({
                "code": code,
                "name": row.get("name", ""),
                "error": f"{type(exc).__name__}: {exc}",
            })
            continue

        future = df[df["date"] > record_date].copy()
        latest = df.iloc[-1]
        buy_close = float(row["latest_close"])

        if future.empty:
            out_row = {
                **row.to_dict(),
                "verify_date": pd.Timestamp(latest["date"]).strftime("%Y-%m-%d"),
                "days_after_record": 0,
                "verify_close": float(latest["close"]),
                "close_return_pct": 0.0,
                "max_high_date": "",
                "max_high_after_record": None,
                "max_gain_pct": 0.0,
                "days_to_max_high": None,
                "min_low_date": "",
                "min_low_after_record": None,
                "max_drawdown_pct": 0.0,
                "error": "",
            }
            for threshold in DEFAULT_HIT_THRESHOLDS:
                out_row[f"hit_{int(threshold)}pct"] = False
            rows.append(out_row)
            continue

        max_high_idx = future["high"].idxmax()
        min_low_idx = future["low"].idxmin()
        verify_close = float(latest["close"])
        max_high = float(df.loc[max_high_idx, "high"])
        min_low = float(df.loc[min_low_idx, "low"])
        max_high_date = pd.Timestamp(df.loc[max_high_idx, "date"])
        min_low_date = pd.Timestamp(df.loc[min_low_idx, "date"])
        max_gain_pct = (max_high - buy_close) / buy_close * 100

        out_row = {
            **row.to_dict(),
            "verify_date": pd.Timestamp(latest["date"]).strftime("%Y-%m-%d"),
            "days_after_record": int((pd.Timestamp(latest["date"]) - record_date).days),
            "verify_close": verify_close,
            "close_return_pct": (verify_close - buy_close) / buy_close * 100,
            "max_high_date": max_high_date.strftime("%Y-%m-%d"),
            "max_high_after_record": max_high,
            "max_gain_pct": max_gain_pct,
            "days_to_max_high": int((max_high_date - record_date).days),
            "min_low_date": min_low_date.strftime("%Y-%m-%d"),
            "min_low_after_record": min_low,
            "max_drawdown_pct": (min_low - buy_close) / buy_close * 100,
            "error": "",
        }
        for threshold in DEFAULT_HIT_THRESHOLDS:
            out_row[f"hit_{int(threshold)}pct"] = max_gain_pct >= threshold
        rows.append(out_row)

    verification = pd.DataFrame(rows)
    out_path = snapshot_dir / f"{out_prefix}_{datetime.now():%Y%m%d_%H%M%S}.xlsx"

    summary_row = build_summary_row(snapshot_dir, verification, record_date)
    summary = pd.DataFrame(summary_row.items(), columns=["项目", "值"])

    with pd.ExcelWriter(out_path) as writer:
        verification.to_excel(writer, index=False, sheet_name="验证结果")
        summary.to_excel(writer, index=False, sheet_name="汇总")

    print(f"已生成验证结果: {out_path.resolve()}")
    print(summary.to_string(index=False))
    return out_path


def verify_snapshot(args: argparse.Namespace) -> Path:
    snapshot_dir = args.snapshot or latest_snapshot(args.record_dir)
    return verify_snapshot_path(snapshot_dir)


def verify_all_snapshots(args: argparse.Namespace) -> list[Path]:
    snapshots = snapshot_dirs(args.record_dir)
    if not snapshots:
        raise FileNotFoundError(f"没有任何记录快照: {args.record_dir}")

    outputs = []
    for snapshot_dir in snapshots:
        print(f"验证快照: {snapshot_dir}")
        outputs.append(verify_snapshot_path(snapshot_dir, out_prefix="verification"))

    print(f"已验证 {len(outputs)} 个快照")
    return outputs


def latest_verification_file(snapshot_dir: Path) -> Path | None:
    files = sorted(snapshot_dir.glob("verification_*.xlsx"))
    if not files:
        return None
    return files[-1]


def summary_all_snapshots(args: argparse.Namespace) -> Path:
    snapshots = snapshot_dirs(args.record_dir)
    if not snapshots:
        raise FileNotFoundError(f"没有任何记录快照: {args.record_dir}")

    rows = []
    for snapshot_dir in snapshots:
        verification_file = latest_verification_file(snapshot_dir)
        if verification_file is None:
            if args.verify_missing:
                verification_file = verify_snapshot_path(snapshot_dir)
            else:
                continue

        verification = pd.read_excel(verification_file, sheet_name="验证结果", dtype={"code": str})
        if verification.empty or "latest_date" not in verification.columns:
            continue
        record_date = pd.to_datetime(verification["latest_date"].iloc[0])
        row = build_summary_row(snapshot_dir, verification, record_date)
        row["verification_file"] = str(verification_file)
        rows.append(row)

    if not rows:
        raise RuntimeError("没有可汇总的验证结果")

    summary = pd.DataFrame(rows).sort_values("record_date").reset_index(drop=True)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with pd.ExcelWriter(args.output) as writer:
        summary.to_excel(writer, index=False, sheet_name="汇总")

    print(f"已生成汇总: {args.output.resolve()}")
    print(summary.to_string(index=False, max_colwidth=80))
    return args.output


def daily_replay_snapshot_dirs(daily_dir: Path, suffix: str | None = None) -> list[Path]:
    if not daily_dir.exists():
        return []
    expected_suffix = f"_{suffix}" if suffix else None
    return sorted(
        (p.parent for p in daily_dir.rglob("ranking_snapshot.csv")
         if expected_suffix is None or p.parent.name.endswith(expected_suffix)),
        key=lambda p: p.name,
    )


def snapshot_record_date(snapshot_dir: Path, ranking: pd.DataFrame) -> pd.Timestamp:
    manifest_path = snapshot_dir / "manifest.json"
    if manifest_path.exists():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            if manifest.get("record_date"):
                return pd.Timestamp(manifest["record_date"])
        except Exception:
            pass

    if "latest_date" not in ranking.columns or ranking.empty:
        raise RuntimeError(f"快照缺少 latest_date: {snapshot_dir}")
    return pd.Timestamp(ranking["latest_date"].iloc[0])


def load_daily_rankings(args: argparse.Namespace) -> tuple[pd.DataFrame, pd.DataFrame]:
    start = pd.Timestamp(args.start_date)
    end = pd.Timestamp(args.end_date)
    rows: list[pd.DataFrame] = []
    skipped: list[dict[str, object]] = []

    snapshot_suffix = getattr(args, "snapshot_suffix", None)
    for snapshot_dir in daily_replay_snapshot_dirs(args.daily_dir, snapshot_suffix):
        ranking_path = snapshot_dir / "ranking_snapshot.csv"
        try:
            ranking = pd.read_csv(ranking_path, dtype={"code": str})
            if ranking.empty:
                skipped.append({"snapshot": snapshot_dir.name, "reason": "ranking empty"})
                continue

            ranking["code"] = ranking["code"].apply(normalize_code)
            record_date = snapshot_record_date(snapshot_dir, ranking)
        except Exception as exc:
            skipped.append({"snapshot": snapshot_dir.name, "reason": f"{type(exc).__name__}: {exc}"})
            continue

        if not (start <= record_date <= end):
            continue

        if "rank" not in ranking.columns:
            skipped.append({"snapshot": snapshot_dir.name, "reason": "missing rank column"})
            continue

        ranking["rank"] = pd.to_numeric(ranking["rank"], errors="coerce")
        ranking.insert(0, "signal_date", record_date.strftime("%Y-%m-%d"))
        ranking.insert(1, "snapshot", snapshot_dir.name)
        rows.append(ranking.sort_values("rank"))

    if rows:
        daily_rankings = pd.concat(rows, ignore_index=True).sort_values(["signal_date", "rank"]).reset_index(drop=True)
    else:
        daily_rankings = pd.DataFrame()

    return daily_rankings, pd.DataFrame(skipped)


def select_daily_signals(daily_rankings: pd.DataFrame, args: argparse.Namespace) -> pd.DataFrame:
    if daily_rankings.empty:
        return daily_rankings.copy()

    if args.selection_mode == "all":
        selected = daily_rankings.copy()
    elif args.selection_mode == "top-n":
        selected = daily_rankings[daily_rankings["rank"] <= args.top_n].copy()
    else:
        if args.min_rank_score is None and args.min_probability_score is None:
            raise RuntimeError("selection-mode=score-threshold 时必须提供 --min-rank-score 或 --min-probability-score")

        mask = pd.Series(False, index=daily_rankings.index)
        if args.min_rank_score is not None:
            mask |= numeric_series(daily_rankings, "rank_score") >= args.min_rank_score
        if args.min_probability_score is not None:
            mask |= numeric_series(daily_rankings, "probability_score") >= args.min_probability_score
        selected = daily_rankings[mask].copy()

    return selected.sort_values(["signal_date", "rank"]).reset_index(drop=True)


def dedupe_monthly_signals(daily_top: pd.DataFrame, dedupe: bool) -> pd.DataFrame:
    if daily_top.empty or not dedupe:
        return daily_top.copy()
    return daily_top.sort_values(["signal_date", "rank"]).drop_duplicates("code", keep="first").reset_index(drop=True)


def verify_monthly_signals(signals: pd.DataFrame, args: argparse.Namespace) -> tuple[pd.DataFrame, pd.DataFrame]:
    rows: list[dict[str, object]] = []
    insufficient: list[dict[str, object]] = []
    price_cache: dict[str, pd.DataFrame] = {}

    for _, row in signals.iterrows():
        code = normalize_code(row["code"])
        try:
            if code not in price_cache:
                price_cache[code] = load_price_frame(code)
            df = price_cache[code]
        except Exception as exc:
            rows.append({
                **row.to_dict(),
                "code": code,
                "error": f"{type(exc).__name__}: {exc}",
            })
            continue

        signal_date = pd.Timestamp(row["signal_date"])
        future = df[df["date"] > signal_date].head(args.lookahead_days).copy()
        buy_close = float(row["latest_close"])
        full_window = len(future) >= args.lookahead_days

        out_row = {
            **row.to_dict(),
            "code": code,
            "buy_close": buy_close,
            "lookahead_days": int(args.lookahead_days),
            "future_trade_days": int(len(future)),
            "full_lookahead_window": bool(full_window),
            "error": "",
        }

        if future.empty:
            out_row.update({
                "verify_end_date": "",
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
        else:
            max_high_idx = future["high"].idxmax()
            min_low_idx = future["low"].idxmin()
            max_high = float(future.loc[max_high_idx, "high"])
            min_low = float(future.loc[min_low_idx, "low"])
            verify_close = float(future.iloc[-1]["close"])
            max_gain_pct = (max_high - buy_close) / buy_close * 100

            out_row.update({
                "verify_end_date": pd.Timestamp(future.iloc[-1]["date"]).strftime("%Y-%m-%d"),
                "max_high_date": pd.Timestamp(future.loc[max_high_idx, "date"]).strftime("%Y-%m-%d"),
                "max_high_after_signal": max_high,
                "max_gain_pct": max_gain_pct,
                "hit_success_threshold": max_gain_pct > args.success_threshold * 100,
                "min_low_date": pd.Timestamp(future.loc[min_low_idx, "date"]).strftime("%Y-%m-%d"),
                "min_low_after_signal": min_low,
                "max_drawdown_pct": (min_low - buy_close) / buy_close * 100,
                "verify_close": verify_close,
                "close_return_pct": (verify_close - buy_close) / buy_close * 100,
            })

        if args.require_full_lookahead and not full_window:
            insufficient.append(out_row)
        else:
            rows.append(out_row)

    return pd.DataFrame(rows), pd.DataFrame(insufficient)


def score_threshold_scan(daily_rankings: pd.DataFrame, args: argparse.Namespace) -> pd.DataFrame:
    if daily_rankings.empty:
        return pd.DataFrame()

    scan_rows: list[dict[str, object]] = []

    for score_column, thresholds in [
        ("rank_score", RANK_SCORE_SCAN_THRESHOLDS),
        ("probability_score", PROBABILITY_SCORE_SCAN_THRESHOLDS),
    ]:
        if score_column not in daily_rankings.columns:
            continue

        score = numeric_series(daily_rankings, score_column)
        candidates = daily_rankings[score >= min(thresholds)].copy()
        if candidates.empty:
            for threshold in thresholds:
                scan_rows.append({
                    "score_column": score_column,
                    "threshold": threshold,
                    "rows": 0,
                    "success_count": 0,
                    "success_rate_pct": 0.0,
                    "avg_max_gain_pct": None,
                    "median_max_gain_pct": None,
                    "insufficient_lookahead_rows": 0,
                })
            continue

        verified_all, insufficient_all = verify_monthly_signals(candidates, args)

        for threshold in thresholds:
            threshold_mask = numeric_series(verified_all, score_column) >= threshold if not verified_all.empty else pd.Series(dtype=bool)
            subset = verified_all[threshold_mask].copy() if not verified_all.empty else verified_all
            if args.dedupe and not subset.empty:
                subset = dedupe_monthly_signals(subset, True)

            if insufficient_all.empty or score_column not in insufficient_all.columns:
                insufficient_count = 0
            else:
                insufficient_subset = insufficient_all[numeric_series(insufficient_all, score_column) >= threshold]
                if args.dedupe and not insufficient_subset.empty:
                    insufficient_subset = dedupe_monthly_signals(insufficient_subset, True)
                insufficient_count = int(len(insufficient_subset))

            hits = subset["hit_success_threshold"].fillna(False).astype(bool) if "hit_success_threshold" in subset else pd.Series(dtype=bool)
            max_gain = numeric_series(subset, "max_gain_pct")
            scan_rows.append({
                "score_column": score_column,
                "threshold": threshold,
                "rows": int(len(subset)),
                "success_count": int(hits.sum()),
                "success_rate_pct": hits.mean() * 100 if len(hits) else 0.0,
                "avg_max_gain_pct": max_gain.mean(),
                "median_max_gain_pct": max_gain.median(),
                "insufficient_lookahead_rows": insufficient_count,
            })

    return pd.DataFrame(scan_rows)


def condition_mask(df: pd.DataFrame, condition: str, args: argparse.Namespace) -> pd.Series:
    if df.empty:
        return pd.Series(dtype=bool)

    mask = pd.Series(True, index=df.index)
    if condition == "filter_a":
        if "latest_turn" not in df.columns or "latest_pct_chg" not in df.columns:
            return pd.Series(False, index=df.index)
        mask &= numeric_series(df, "latest_turn") >= args.min_latest_turn
        mask &= numeric_series(df, "latest_pct_chg") <= args.max_latest_pct_chg
    elif condition == "filter_b":
        if "current_drawdown_pct" not in df.columns or "latest_turn" not in df.columns:
            return pd.Series(False, index=df.index)
        mask &= numeric_series(df, "current_drawdown_pct") >= args.min_current_drawdown
        mask &= numeric_series(df, "latest_turn") >= args.min_latest_turn
    return mask


def selection_description(args: argparse.Namespace) -> str:
    if args.selection_mode == "all":
        return "全部排行"

    if args.selection_mode == "top-n":
        return f"rank <= {args.top_n}"

    parts = []
    if args.min_rank_score is not None:
        parts.append(f"rank_score >= {args.min_rank_score:g}")
    if args.min_probability_score is not None:
        parts.append(f"probability_score >= {args.min_probability_score:g}")
    return " or ".join(parts)


def monthly_summary_rows(verified: pd.DataFrame, insufficient: pd.DataFrame, selected_signals: pd.DataFrame, daily_rankings: pd.DataFrame, args: argparse.Namespace) -> pd.DataFrame:
    rows: list[dict[str, object]] = []
    conditions = [
        ("原始模型", "base"),
        (f"过滤A: latest_turn>={args.min_latest_turn:g}, latest_pct_chg<={args.max_latest_pct_chg:g}", "filter_a"),
        (f"过滤B: current_drawdown_pct>={args.min_current_drawdown:g}, latest_turn>={args.min_latest_turn:g}", "filter_b"),
    ]

    for label, condition in conditions:
        if verified.empty:
            subset = verified
        else:
            subset = verified[condition_mask(verified, condition, args)]

        hits = subset["hit_success_threshold"].fillna(False).astype(bool) if "hit_success_threshold" in subset else pd.Series(dtype=bool)
        max_gain = numeric_series(subset, "max_gain_pct")
        max_drawdown = numeric_series(subset, "max_drawdown_pct")

        rows.append({
            "分组": label,
            "start_date": args.start_date,
            "end_date": args.end_date,
            "top_n": int(args.top_n),
            "selection_mode": args.selection_mode,
            "selection": selection_description(args),
            "daily_ranking_rows": int(len(daily_rankings)),
            "selected_signal_rows": int(len(selected_signals)),
            "dedupe": bool(args.dedupe),
            "require_full_lookahead": bool(args.require_full_lookahead),
            "rows": int(len(subset)),
            "insufficient_lookahead_rows": int(len(insufficient)),
            "success_threshold_pct": args.success_threshold * 100,
            "lookahead_days": int(args.lookahead_days),
            "success_count": int(hits.sum()),
            "success_rate_pct": hits.mean() * 100 if len(hits) else 0.0,
            "avg_max_gain_pct": max_gain.mean(),
            "median_max_gain_pct": max_gain.median(),
            "avg_max_drawdown_pct": max_drawdown.mean(),
        })

    return pd.DataFrame(rows)


def monthly_explanation(args: argparse.Namespace) -> pd.DataFrame:
    rows = [
        ("数据来源", f"从 {args.daily_dir} 下的每日 ranking_snapshot.csv 读取，不重新筛选全市场。"),
        ("选择模式", f"{args.selection_mode}: {selection_description(args)}。"),
        ("去重", "默认同一股票只保留日期最早、排名最靠前的一次。" if args.dedupe else "不去重，每日信号都参与统计。"),
        ("成功标准", f"入选日收盘价买入，之后 {args.lookahead_days} 个交易日内最高涨幅 > {args.success_threshold * 100:.0f}% 算成功。"),
        ("未来不足窗口", "默认不计入主胜率，单独输出到 未来不足20日 sheet。" if args.require_full_lookahead else "计入主胜率。"),
        ("过滤A", f"latest_turn >= {args.min_latest_turn:g} 且 latest_pct_chg <= {args.max_latest_pct_chg:g}。"),
        ("过滤B", f"current_drawdown_pct >= {args.min_current_drawdown:g} 且 latest_turn >= {args.min_latest_turn:g}。"),
        ("分数阈值扫描", "自动扫描 rank_score 与 probability_score 的常用门槛，统计样本数、成功数和胜率。"),
    ]
    return pd.DataFrame(rows, columns=["项目", "说明"])


def default_monthly_output(args: argparse.Namespace) -> Path:
    start = str(args.start_date).replace("-", "")
    end = str(args.end_date).replace("-", "")
    mode = "score_threshold" if args.selection_mode == "score-threshold" else f"top{args.top_n}"
    return MONTHLY_ANALYSIS_DIR / f"rebound_{mode}_{start}_{end}.xlsx"


def analyze_month(args: argparse.Namespace) -> Path:
    daily_rankings, skipped = load_daily_rankings(args)
    if daily_rankings.empty:
        raise RuntimeError(f"没有从 {args.daily_dir} 找到 {args.start_date} 到 {args.end_date} 的每日排行快照")

    selected_signals = select_daily_signals(daily_rankings, args)
    if selected_signals.empty:
        raise RuntimeError(f"{args.start_date} 到 {args.end_date} 没有符合 {selection_description(args)} 的信号")

    signals = dedupe_monthly_signals(selected_signals, args.dedupe)
    verified, insufficient = verify_monthly_signals(signals, args)
    summary = monthly_summary_rows(verified, insufficient, selected_signals, daily_rankings, args)
    threshold_scan = score_threshold_scan(daily_rankings, args)

    output = args.output or default_monthly_output(args)
    output.parent.mkdir(parents=True, exist_ok=True)

    with pd.ExcelWriter(output) as writer:
        summary.to_excel(writer, index=False, sheet_name="汇总对比")
        verified.to_excel(writer, index=False, sheet_name="去重入选明细" if args.dedupe else "入选明细")
        selected_signals.to_excel(writer, index=False, sheet_name="每日入选原始信号")
        daily_rankings.to_excel(writer, index=False, sheet_name="每日完整排行")
        threshold_scan.to_excel(writer, index=False, sheet_name="分数阈值扫描")
        insufficient.to_excel(writer, index=False, sheet_name="未来不足20日")
        skipped.to_excel(writer, index=False, sheet_name="跳过快照")
        monthly_explanation(args).to_excel(writer, index=False, sheet_name="参数说明")

    print(f"已生成月度分析: {output.resolve()}")
    print(summary.to_string(index=False, max_colwidth=100))
    return output


def portfolio_selection_description(args: argparse.Namespace) -> str:
    parts = [
        selection_description(args),
        f"latest_turn >= {args.min_latest_turn:g}",
        f"current_drawdown_pct <= {args.max_current_drawdown:g}",
        f"每日候选最多 {args.max_daily_candidates} 只",
    ]
    if getattr(args, "min_current_drawdown", None) is not None:
        parts.insert(2, f"current_drawdown_pct >= {args.min_current_drawdown:g}")
    if getattr(args, "min_rank_score", None) is not None:
        parts.append(f"rank_score >= {args.min_rank_score:g}")
    if getattr(args, "max_latest_pct_chg", None) is not None:
        parts.append(f"latest_pct_chg <= {args.max_latest_pct_chg:g}")
    if getattr(args, "min_latest_pct_chg", None) is not None:
        parts.append(f"latest_pct_chg >= {args.min_latest_pct_chg:g}")
    if getattr(args, "max_low_to_latest_pct", None) is not None:
        parts.append(f"current_low_to_latest_pct <= {args.max_low_to_latest_pct:g}")
    if getattr(args, "min_close_drawdown", None) is not None:
        parts.append(f"close_drawdown_pct >= {args.min_close_drawdown:g}")
    if getattr(args, "max_close_drawdown", None) is not None:
        parts.append(f"close_drawdown_pct <= {args.max_close_drawdown:g}")
    if getattr(args, "min_next_open_to_signal_close_pct", None) is not None:
        parts.append(f"next_open_to_signal_close_pct >= {args.min_next_open_to_signal_close_pct:g}")
    if getattr(args, "max_next_open_to_signal_close_pct", None) is not None:
        parts.append(f"next_open_to_signal_close_pct <= {args.max_next_open_to_signal_close_pct:g}")
    if getattr(args, "max_next_open_to_current_low_pct", None) is not None:
        parts.append(f"next_open_to_current_low_pct <= {args.max_next_open_to_current_low_pct:g}")
    if getattr(args, "max_rank", None) is not None:
        parts.append(f"rank <= {args.max_rank:g}")
    return "; ".join(parts)


def select_portfolio_candidates(daily_rankings: pd.DataFrame, args: argparse.Namespace) -> pd.DataFrame:
    selected = select_daily_signals(daily_rankings, args)
    if selected.empty:
        return selected
    selected = with_close_drawdown_pct(selected)

    required_columns = ["latest_turn", "current_drawdown_pct"]
    if getattr(args, "min_close_drawdown", None) is not None or getattr(args, "max_close_drawdown", None) is not None:
        required_columns.append("close_drawdown_pct")
    missing = [column for column in required_columns if column not in selected.columns]
    if missing:
        raise RuntimeError(f"每日排行缺少组合回测过滤字段: {', '.join(missing)}")

    mask = pd.Series(True, index=selected.index)
    mask &= numeric_series(selected, "latest_turn") >= args.min_latest_turn
    if getattr(args, "min_current_drawdown", None) is not None:
        mask &= numeric_series(selected, "current_drawdown_pct") >= args.min_current_drawdown
    mask &= numeric_series(selected, "current_drawdown_pct") <= args.max_current_drawdown
    if getattr(args, "min_rank_score", None) is not None:
        mask &= numeric_series(selected, "rank_score") >= args.min_rank_score
    if getattr(args, "max_latest_pct_chg", None) is not None:
        mask &= numeric_series(selected, "latest_pct_chg") <= args.max_latest_pct_chg
    if getattr(args, "min_latest_pct_chg", None) is not None:
        mask &= numeric_series(selected, "latest_pct_chg") >= args.min_latest_pct_chg
    if getattr(args, "max_low_to_latest_pct", None) is not None:
        mask &= numeric_series(selected, "current_low_to_latest_pct") <= args.max_low_to_latest_pct
    if getattr(args, "min_close_drawdown", None) is not None:
        mask &= numeric_series(selected, "close_drawdown_pct") >= args.min_close_drawdown
    if getattr(args, "max_close_drawdown", None) is not None:
        mask &= numeric_series(selected, "close_drawdown_pct") <= args.max_close_drawdown
    if getattr(args, "max_rank", None) is not None:
        mask &= numeric_series(selected, "rank") <= args.max_rank
    candidates = selected[mask].copy().sort_values(["signal_date", "rank"])

    min_daily = getattr(args, "min_daily_candidates", None)
    if min_daily is not None and not candidates.empty:
        daily_counts = candidates.groupby("signal_date").size()
        valid_signal_dates = daily_counts[daily_counts >= min_daily].index
        candidates = candidates[candidates["signal_date"].isin(valid_signal_dates)]

    if args.max_daily_candidates > 0:
        candidates = candidates.groupby("signal_date", group_keys=False).head(args.max_daily_candidates)

    return candidates.reset_index(drop=True)


def next_trade_row(df: pd.DataFrame, date: pd.Timestamp) -> pd.Series | None:
    future = df[df["date"] > date]
    if future.empty:
        return None
    return future.iloc[0]


def cooldown_until_date(df: pd.DataFrame, exit_date: pd.Timestamp, cooldown_days: int) -> pd.Timestamp:
    if cooldown_days <= 0:
        return exit_date

    future = df[df["date"] > exit_date].head(cooldown_days)
    if future.empty:
        return pd.Timestamp.max
    return pd.Timestamp(future.iloc[-1]["date"])


def build_portfolio_orders(
    candidates: pd.DataFrame,
    price_cache: dict[str, pd.DataFrame],
    args: argparse.Namespace,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    orders: list[dict[str, object]] = []
    skipped: list[dict[str, object]] = []

    for _, row in candidates.iterrows():
        code = normalize_code(row["code"])
        signal_date = pd.Timestamp(row["signal_date"])

        try:
            if code not in price_cache:
                price_cache[code] = load_price_frame(code)
            df = price_cache[code]
        except Exception as exc:
            skipped.append({
                **row.to_dict(),
                "code": code,
                "skip_reason": f"行情读取失败: {type(exc).__name__}: {exc}",
            })
            continue

        entry_row = next_trade_row(df, signal_date)
        if entry_row is None:
            skipped.append({**row.to_dict(), "code": code, "skip_reason": "没有次日交易行情"})
            continue

        entry_open = pd.to_numeric(pd.Series([entry_row.get("open")]), errors="coerce").iloc[0]
        if pd.isna(entry_open) or float(entry_open) <= 0:
            skipped.append({**row.to_dict(), "code": code, "skip_reason": "次日开盘价缺失"})
            continue

        entry_open = float(entry_open)
        signal_close = safe_float(row.get("latest_close"))
        current_low_price = safe_float(row.get("current_low_price"))
        next_open_to_signal_close_pct = (
            (entry_open - signal_close) / signal_close * 100
            if signal_close > 0
            else None
        )
        next_open_to_current_low_pct = (
            (entry_open - current_low_price) / current_low_price * 100
            if current_low_price > 0
            else None
        )

        max_to_signal_close = getattr(args, "max_next_open_to_signal_close_pct", None)
        if max_to_signal_close is not None:
            if next_open_to_signal_close_pct is None:
                skipped.append({**row.to_dict(), "code": code, "skip_reason": "次日开盘相对信号收盘过滤基准缺失"})
                continue
            if next_open_to_signal_close_pct > float(max_to_signal_close):
                skipped.append({
                    **row.to_dict(),
                    "code": code,
                    "entry_date": pd.Timestamp(entry_row["date"]).strftime("%Y-%m-%d"),
                    "entry_open": entry_open,
                    "next_open_to_signal_close_pct": next_open_to_signal_close_pct,
                    "next_open_to_current_low_pct": next_open_to_current_low_pct,
                    "skip_reason": f"次日开盘相对信号收盘涨幅 {next_open_to_signal_close_pct:.2f}% > {float(max_to_signal_close):g}%",
                })
                continue

        min_to_signal_close = getattr(args, "min_next_open_to_signal_close_pct", None)
        if min_to_signal_close is not None:
            if next_open_to_signal_close_pct is None:
                skipped.append({**row.to_dict(), "code": code, "skip_reason": "次日开盘相对信号收盘过滤基准缺失"})
                continue
            if next_open_to_signal_close_pct < float(min_to_signal_close):
                skipped.append({
                    **row.to_dict(),
                    "code": code,
                    "entry_date": pd.Timestamp(entry_row["date"]).strftime("%Y-%m-%d"),
                    "entry_open": entry_open,
                    "next_open_to_signal_close_pct": next_open_to_signal_close_pct,
                    "next_open_to_current_low_pct": next_open_to_current_low_pct,
                    "skip_reason": f"次日开盘相对信号收盘跌幅 {next_open_to_signal_close_pct:.2f}% < {float(min_to_signal_close):g}%",
                })
                continue

        max_to_current_low = getattr(args, "max_next_open_to_current_low_pct", None)
        if max_to_current_low is not None:
            if next_open_to_current_low_pct is None:
                skipped.append({**row.to_dict(), "code": code, "skip_reason": "次日开盘相对当前低点过滤基准缺失"})
                continue
            if next_open_to_current_low_pct > float(max_to_current_low):
                skipped.append({
                    **row.to_dict(),
                    "code": code,
                    "entry_date": pd.Timestamp(entry_row["date"]).strftime("%Y-%m-%d"),
                    "entry_open": entry_open,
                    "next_open_to_signal_close_pct": next_open_to_signal_close_pct,
                    "next_open_to_current_low_pct": next_open_to_current_low_pct,
                    "skip_reason": f"次日开盘相对当前低点涨幅 {next_open_to_current_low_pct:.2f}% > {float(max_to_current_low):g}%",
                })
                continue

        orders.append({
            **row.to_dict(),
            "code": code,
            "entry_date": pd.Timestamp(entry_row["date"]).strftime("%Y-%m-%d"),
            "entry_open": entry_open,
            "next_open_to_signal_close_pct": next_open_to_signal_close_pct,
            "next_open_to_current_low_pct": next_open_to_current_low_pct,
        })

    if orders:
        orders_df = pd.DataFrame(orders).sort_values(["entry_date", "signal_date", "rank"]).reset_index(drop=True)
    else:
        orders_df = pd.DataFrame()

    return orders_df, pd.DataFrame(skipped)


def price_lookup_for_codes(price_cache: dict[str, pd.DataFrame]) -> dict[str, dict[pd.Timestamp, pd.Series]]:
    lookup: dict[str, dict[pd.Timestamp, pd.Series]] = {}
    for code, df in price_cache.items():
        lookup[code] = {pd.Timestamp(row["date"]): row for _, row in df.iterrows()}
    return lookup


def portfolio_trading_dates(
    price_cache: dict[str, pd.DataFrame],
    orders: pd.DataFrame,
    args: argparse.Namespace,
) -> list[pd.Timestamp]:
    if orders.empty:
        return []

    start = pd.Timestamp(orders["entry_date"].min())
    end = pd.Timestamp(args.end_date) + pd.Timedelta(days=args.max_hold_days * 4 + args.reentry_cooldown_days * 3 + 10)
    dates: set[pd.Timestamp] = set()
    ordered_codes = set(orders["code"].astype(str))

    for code in ordered_codes:
        df = price_cache.get(code)
        if df is None:
            continue
        window = df[(df["date"] >= start) & (df["date"] <= end)]
        dates.update(pd.Timestamp(value) for value in window["date"].tolist())

    return sorted(dates)


def close_trade(
    holding: dict[str, object],
    exit_date: pd.Timestamp,
    exit_price: float,
    exit_reason: str,
    exit_reason_cn: str,
    holding_days: int,
) -> dict[str, object]:
    entry_price = float(holding["entry_price"])
    return_pct = (exit_price - entry_price) / entry_price * 100
    row = {
        **holding["signal"].to_dict(),
        "code": holding["code"],
        "name": holding.get("name", ""),
        "signal_date": pd.Timestamp(holding["signal_date"]).strftime("%Y-%m-%d"),
        "entry_date": pd.Timestamp(holding["entry_date"]).strftime("%Y-%m-%d"),
        "entry_price": entry_price,
        "exit_date": exit_date.strftime("%Y-%m-%d"),
        "exit_price": exit_price,
        "exit_reason": exit_reason,
        "exit_reason_cn": exit_reason_cn,
        "holding_days": int(holding_days),
        "return_pct": return_pct,
        "is_win": return_pct > 0,
        "is_take_profit": exit_reason == "take_profit",
    }
    return row


def simulate_portfolio_trades(
    orders: pd.DataFrame,
    price_cache: dict[str, pd.DataFrame],
    args: argparse.Namespace,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    if orders.empty:
        return pd.DataFrame(), pd.DataFrame(), pd.DataFrame()

    price_lookup = getattr(args, "_price_lookup", None) or price_lookup_for_codes(price_cache)
    dates = getattr(args, "_trading_dates", None) or portfolio_trading_dates(price_cache, orders, args)
    orders_by_entry = {date: group.copy() for date, group in orders.groupby("entry_date")}

    holdings: list[dict[str, object]] = []
    trades: list[dict[str, object]] = []
    daily_rows: list[dict[str, object]] = []
    skipped: list[dict[str, object]] = []
    cooldown_until: dict[str, pd.Timestamp] = {}

    for trade_date in dates:
        trade_date_str = trade_date.strftime("%Y-%m-%d")
        start_codes = [str(holding["code"]) for holding in holdings]
        entries_today: list[str] = []
        exits_today: list[str] = []
        start_holding_count = len(holdings)

        todays_orders = orders_by_entry.get(trade_date_str, pd.DataFrame())
        daily_buy_count = 0
        for _, order in todays_orders.iterrows():
            code = normalize_code(order["code"])
            if daily_buy_count >= args.max_daily_buys:
                skipped.append({**order.to_dict(), "skip_reason": "达到每日买入上限"})
                continue
            if len(holdings) >= args.max_positions:
                skipped.append({**order.to_dict(), "skip_reason": "满仓"})
                continue
            if any(str(holding["code"]) == code for holding in holdings):
                skipped.append({**order.to_dict(), "skip_reason": "已持有同一股票"})
                continue
            if code in cooldown_until and trade_date <= cooldown_until[code]:
                skipped.append({
                    **order.to_dict(),
                    "skip_reason": f"重复股票冷却期至 {cooldown_until[code].strftime('%Y-%m-%d')}",
                })
                continue

            entry_price = float(order["entry_open"])
            holdings.append({
                "code": code,
                "name": order.get("name", ""),
                "signal": order,
                "signal_date": pd.Timestamp(order["signal_date"]),
                "entry_date": trade_date,
                "entry_price": entry_price,
                "take_profit_price": entry_price * (1 + args.take_profit),
                "stop_loss_price": entry_price * (1 - args.stop_loss),
                "holding_days": 0,
            })
            entries_today.append(code)
            daily_buy_count += 1

        remaining: list[dict[str, object]] = []
        for holding in holdings:
            code = str(holding["code"])
            row = price_lookup.get(code, {}).get(trade_date)
            if row is None:
                remaining.append(holding)
                continue

            holding["holding_days"] = int(holding["holding_days"]) + 1
            holding_days = int(holding["holding_days"])
            high = float(row["high"])
            low = float(row["low"])
            close = float(row["close"])

            exit_trade: dict[str, object] | None = None
            if low <= float(holding["stop_loss_price"]):
                exit_trade = close_trade(
                    holding,
                    trade_date,
                    float(holding["stop_loss_price"]),
                    "stop_loss",
                    "止损",
                    holding_days,
                )
            elif high >= float(holding["take_profit_price"]):
                exit_trade = close_trade(
                    holding,
                    trade_date,
                    float(holding["take_profit_price"]),
                    "take_profit",
                    "止盈",
                    holding_days,
                )
            elif holding_days >= args.max_hold_days:
                exit_trade = close_trade(holding, trade_date, close, "time_exit", "到期卖出", holding_days)

            if exit_trade is None:
                remaining.append(holding)
            else:
                trades.append(exit_trade)
                exits_today.append(code)
                cooldown_until[code] = cooldown_until_date(price_cache[code], trade_date, args.reentry_cooldown_days)

        holdings = remaining
        daily_rows.append({
            "date": trade_date_str,
            "start_holding_count": start_holding_count,
            "start_codes": ",".join(start_codes),
            "entries": ",".join(entries_today),
            "exits": ",".join(exits_today),
            "end_holding_count": len(holdings),
            "end_codes": ",".join(str(holding["code"]) for holding in holdings),
        })

    for holding in holdings:
        code = str(holding["code"])
        skipped.append({
            **holding["signal"].to_dict(),
            "code": code,
            "skip_reason": "回测结束仍持仓未退出",
        })

    return pd.DataFrame(trades), pd.DataFrame(daily_rows), pd.DataFrame(skipped)


def portfolio_summary(
    trades: pd.DataFrame,
    daily_positions: pd.DataFrame,
    candidates: pd.DataFrame,
    orders: pd.DataFrame,
    skipped: pd.DataFrame,
    args: argparse.Namespace,
) -> pd.DataFrame:
    returns = numeric_series(trades, "return_pct") if not trades.empty else pd.Series(dtype=float)
    wins = trades["is_win"].fillna(False).astype(bool) if "is_win" in trades else pd.Series(dtype=bool)
    take_profit = trades["is_take_profit"].fillna(False).astype(bool) if "is_take_profit" in trades else pd.Series(dtype=bool)
    exit_reason = trades["exit_reason"] if "exit_reason" in trades else pd.Series(dtype=object)
    holding_days = numeric_series(trades, "holding_days") if not trades.empty else pd.Series(dtype=float)
    max_holding = numeric_series(daily_positions, "end_holding_count").max() if not daily_positions.empty else 0

    rows = [
        ("start_date", args.start_date),
        ("end_date", args.end_date),
        ("selection", portfolio_selection_description(args)),
        ("max_positions", int(args.max_positions)),
        ("max_daily_buys", int(args.max_daily_buys)),
        ("take_profit_pct", args.take_profit * 100),
        ("stop_loss_pct", args.stop_loss * 100),
        ("max_hold_days", int(args.max_hold_days)),
        ("reentry_cooldown_days", int(args.reentry_cooldown_days)),
        ("candidate_rows", int(len(candidates))),
        ("valid_next_open_orders", int(len(orders))),
        ("trade_count", int(len(trades))),
        ("win_count", int(wins.sum())),
        ("win_rate_pct", wins.mean() * 100 if len(wins) else 0.0),
        ("take_profit_count", int(take_profit.sum())),
        ("take_profit_rate_pct", take_profit.mean() * 100 if len(take_profit) else 0.0),
        ("stop_loss_count", int((exit_reason == "stop_loss").sum())),
        ("time_exit_count", int((exit_reason == "time_exit").sum())),
        ("avg_return_pct", returns.mean()),
        ("median_return_pct", returns.median()),
        ("sum_trade_return_pct", returns.sum()),
        ("simple_slot_adjusted_return_pct", returns.sum() / args.max_positions if args.max_positions else returns.sum()),
        ("avg_holding_days", holding_days.mean()),
        ("median_holding_days", holding_days.median()),
        ("max_observed_positions", int(max_holding) if pd.notna(max_holding) else 0),
        ("skipped_count", int(len(skipped))),
    ]
    return pd.DataFrame(rows, columns=["项目", "值"])


def portfolio_explanation(args: argparse.Namespace) -> pd.DataFrame:
    rows = [
        ("数据来源", f"从 {args.daily_dir} 下的每日 ranking_snapshot.csv 读取，不重新筛选全市场。"),
        ("入选条件", portfolio_selection_description(args)),
        ("买入价", "信号日后的第一个交易日开盘价。"),
        ("仓位限制", f"最多同时持有 {args.max_positions} 只；每日最多新买 {args.max_daily_buys} 只。"),
        ("止盈", f"盘中最高价达到买入价 +{args.take_profit * 100:.0f}% 时卖出。"),
        ("止损", f"盘中最低价达到买入价 -{args.stop_loss * 100:.0f}% 时卖出。"),
        ("同日触发", "如果同一天止盈和止损都可能触发，按保守口径止损优先。"),
        ("持有期限", f"最多持有 {args.max_hold_days} 个交易日，到期按收盘价卖出。"),
        ("重复股票", f"卖出后间隔 {args.reentry_cooldown_days} 个交易日才允许再次买入。"),
        ("胜率", "按实际完成交易的 return_pct > 0 统计；止盈率单独统计。"),
        ("资金回测", f"初始本金 {args.initial_capital:g}，每手 {args.lot_size} 股，使用可用现金尽量买满整手，不扣交易费用。"),
    ]
    return pd.DataFrame(rows, columns=["项目", "说明"])


def trade_timeline(trades: pd.DataFrame) -> pd.DataFrame:
    columns = [
        "找到日期",
        "股票代码",
        "股票名称",
        "排名",
        "买入日期",
        "买入价格",
        "卖出日期",
        "卖出价格",
        "卖出原因",
        "持有交易日",
        "收益率_pct",
    ]
    if trades.empty:
        return pd.DataFrame(columns=columns)

    timeline = pd.DataFrame({
        "找到日期": trades.get("signal_date", ""),
        "股票代码": trades.get("code", "").astype(str).str.zfill(6),
        "股票名称": trades.get("name", ""),
        "排名": pd.to_numeric(trades.get("rank", pd.Series(dtype=float)), errors="coerce"),
        "买入日期": trades.get("entry_date", ""),
        "买入价格": pd.to_numeric(trades.get("entry_price", pd.Series(dtype=float)), errors="coerce").round(4),
        "卖出日期": trades.get("exit_date", ""),
        "卖出价格": pd.to_numeric(trades.get("exit_price", pd.Series(dtype=float)), errors="coerce").round(4),
        "卖出原因": trades.get("exit_reason_cn", ""),
        "持有交易日": pd.to_numeric(trades.get("holding_days", pd.Series(dtype=float)), errors="coerce"),
        "收益率_pct": pd.to_numeric(trades.get("return_pct", pd.Series(dtype=float)), errors="coerce").round(4),
    })
    return timeline.sort_values(["找到日期", "买入日期", "排名", "股票代码"]).reset_index(drop=True)


def cash_trade_timeline(trades: pd.DataFrame) -> pd.DataFrame:
    columns = [
        "找到日期",
        "股票代码",
        "股票名称",
        "排名",
        "买入日期",
        "买入价格",
        "买入股数",
        "买入金额",
        "卖出日期",
        "卖出价格",
        "卖出金额",
        "卖出原因",
        "持有交易日",
        "单笔盈亏",
        "单笔收益率_pct",
        "卖出后现金",
    ]
    if trades.empty:
        return pd.DataFrame(columns=columns)

    timeline = pd.DataFrame({
        "找到日期": trades.get("signal_date", ""),
        "股票代码": trades.get("code", "").astype(str).str.zfill(6),
        "股票名称": trades.get("name", ""),
        "排名": pd.to_numeric(trades.get("rank", pd.Series(dtype=float)), errors="coerce"),
        "买入日期": trades.get("entry_date", ""),
        "买入价格": pd.to_numeric(trades.get("entry_price", pd.Series(dtype=float)), errors="coerce").round(4),
        "买入股数": pd.to_numeric(trades.get("quantity", pd.Series(dtype=float)), errors="coerce"),
        "买入金额": pd.to_numeric(trades.get("buy_amount", pd.Series(dtype=float)), errors="coerce").round(2),
        "卖出日期": trades.get("exit_date", ""),
        "卖出价格": pd.to_numeric(trades.get("exit_price", pd.Series(dtype=float)), errors="coerce").round(4),
        "卖出金额": pd.to_numeric(trades.get("sell_amount", pd.Series(dtype=float)), errors="coerce").round(2),
        "卖出原因": trades.get("exit_reason_cn", ""),
        "持有交易日": pd.to_numeric(trades.get("holding_days", pd.Series(dtype=float)), errors="coerce"),
        "单笔盈亏": pd.to_numeric(trades.get("pnl", pd.Series(dtype=float)), errors="coerce").round(2),
        "单笔收益率_pct": pd.to_numeric(trades.get("return_pct", pd.Series(dtype=float)), errors="coerce").round(4),
        "卖出后现金": pd.to_numeric(trades.get("cash_after_exit", pd.Series(dtype=float)), errors="coerce").round(2),
    })
    return timeline.sort_values(["找到日期", "买入日期", "排名", "股票代码"]).reset_index(drop=True)


def close_cash_trade(
    holding: dict[str, object],
    exit_date: pd.Timestamp,
    exit_price: float,
    exit_reason: str,
    exit_reason_cn: str,
    holding_days: int,
    cash_after_exit: float,
) -> dict[str, object]:
    entry_price = float(holding["entry_price"])
    quantity = int(holding["quantity"])
    buy_amount = entry_price * quantity
    sell_amount = exit_price * quantity
    pnl = sell_amount - buy_amount
    return {
        **holding["signal"].to_dict(),
        "code": holding["code"],
        "name": holding.get("name", ""),
        "signal_date": pd.Timestamp(holding["signal_date"]).strftime("%Y-%m-%d"),
        "entry_date": pd.Timestamp(holding["entry_date"]).strftime("%Y-%m-%d"),
        "entry_price": entry_price,
        "quantity": quantity,
        "buy_amount": buy_amount,
        "cash_after_entry": float(holding["cash_after_entry"]),
        "exit_date": exit_date.strftime("%Y-%m-%d"),
        "exit_price": exit_price,
        "sell_amount": sell_amount,
        "cash_after_exit": cash_after_exit,
        "exit_reason": exit_reason,
        "exit_reason_cn": exit_reason_cn,
        "holding_days": int(holding_days),
        "pnl": pnl,
        "return_pct": pnl / buy_amount * 100 if buy_amount else 0.0,
        "is_win": pnl > 0,
        "is_take_profit": exit_reason == "take_profit",
    }


def simulate_cash_portfolio(
    orders: pd.DataFrame,
    price_cache: dict[str, pd.DataFrame],
    args: argparse.Namespace,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    if orders.empty:
        summary = cash_portfolio_summary(pd.DataFrame(), pd.DataFrame(), pd.DataFrame(), args)
        return pd.DataFrame(), pd.DataFrame(), pd.DataFrame(), summary

    price_lookup = getattr(args, "_price_lookup", None) or price_lookup_for_codes(price_cache)
    dates = getattr(args, "_trading_dates", None) or portfolio_trading_dates(price_cache, orders, args)
    orders_by_entry = {date: group.copy() for date, group in orders.groupby("entry_date")}

    cash = float(args.initial_capital)
    holdings: list[dict[str, object]] = []
    trades: list[dict[str, object]] = []
    daily_assets: list[dict[str, object]] = []
    skipped: list[dict[str, object]] = []
    cooldown_until: dict[str, pd.Timestamp] = {}

    for trade_date in dates:
        trade_date_str = trade_date.strftime("%Y-%m-%d")
        start_cash = cash
        start_codes = [str(holding["code"]) for holding in holdings]
        entries_today: list[str] = []
        exits_today: list[str] = []

        todays_orders = orders_by_entry.get(trade_date_str, pd.DataFrame())
        daily_buy_count = 0
        for _, order in todays_orders.iterrows():
            code = normalize_code(order["code"])
            if daily_buy_count >= args.max_daily_buys:
                skipped.append({**order.to_dict(), "skip_reason": "达到每日买入上限", "cash": cash})
                continue
            if len(holdings) >= args.max_positions:
                skipped.append({**order.to_dict(), "skip_reason": "满仓", "cash": cash})
                continue
            if any(str(holding["code"]) == code for holding in holdings):
                skipped.append({**order.to_dict(), "skip_reason": "已持有同一股票", "cash": cash})
                continue
            if code in cooldown_until and trade_date <= cooldown_until[code]:
                skipped.append({
                    **order.to_dict(),
                    "skip_reason": f"重复股票冷却期至 {cooldown_until[code].strftime('%Y-%m-%d')}",
                    "cash": cash,
                })
                continue

            entry_price = float(order["entry_open"])
            lot_cost = entry_price * int(args.lot_size)
            lots = int(cash // lot_cost) if lot_cost > 0 else 0
            quantity = lots * int(args.lot_size)
            if quantity < int(args.lot_size):
                skipped.append({
                    **order.to_dict(),
                    "skip_reason": "现金不足买1手",
                    "cash": cash,
                    "lot_cost": lot_cost,
                })
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
                "cash_after_entry": cash,
                "take_profit_price": entry_price * (1 + args.take_profit),
                "stop_loss_price": entry_price * (1 - args.stop_loss),
                "holding_days": 0,
            })
            entries_today.append(code)
            daily_buy_count += 1

        remaining: list[dict[str, object]] = []
        for holding in holdings:
            code = str(holding["code"])
            row = price_lookup.get(code, {}).get(trade_date)
            if row is None:
                remaining.append(holding)
                continue

            holding["holding_days"] = int(holding["holding_days"]) + 1
            holding_days = int(holding["holding_days"])
            high = float(row["high"])
            low = float(row["low"])
            close = float(row["close"])

            exit_price: float | None = None
            exit_reason: str | None = None
            exit_reason_cn: str | None = None
            if low <= float(holding["stop_loss_price"]):
                exit_price = float(holding["stop_loss_price"])
                exit_reason = "stop_loss"
                exit_reason_cn = "止损"
            elif high >= float(holding["take_profit_price"]):
                exit_price = float(holding["take_profit_price"])
                exit_reason = "take_profit"
                exit_reason_cn = "止盈"
            elif holding_days >= args.max_hold_days:
                exit_price = close
                exit_reason = "time_exit"
                exit_reason_cn = "到期卖出"

            if exit_price is None or exit_reason is None or exit_reason_cn is None:
                remaining.append(holding)
            else:
                sell_amount = exit_price * int(holding["quantity"])
                cash += sell_amount
                trades.append(close_cash_trade(
                    holding,
                    trade_date,
                    exit_price,
                    exit_reason,
                    exit_reason_cn,
                    holding_days,
                    cash,
                ))
                exits_today.append(code)
                cooldown_until[code] = cooldown_until_date(price_cache[code], trade_date, args.reentry_cooldown_days)

        holdings = remaining
        market_value = 0.0
        holding_parts: list[str] = []
        for holding in holdings:
            code = str(holding["code"])
            row = price_lookup.get(code, {}).get(trade_date)
            mark_price = float(row["close"]) if row is not None else float(holding["entry_price"])
            value = mark_price * int(holding["quantity"])
            market_value += value
            holding_parts.append(f"{code}:{int(holding['quantity'])}")

        total_asset = cash + market_value
        daily_assets.append({
            "date": trade_date_str,
            "start_cash": start_cash,
            "cash": cash,
            "market_value": market_value,
            "total_asset": total_asset,
            "holding_count": len(holdings),
            "holdings": ",".join(holding_parts),
            "start_codes": ",".join(start_codes),
            "entries": ",".join(entries_today),
            "exits": ",".join(exits_today),
        })

    for holding in holdings:
        code = str(holding["code"])
        skipped.append({
            **holding["signal"].to_dict(),
            "code": code,
            "skip_reason": "回测结束仍持仓未退出",
            "cash": cash,
        })

    trades_df = pd.DataFrame(trades)
    daily_assets_df = pd.DataFrame(daily_assets)
    skipped_df = pd.DataFrame(skipped)
    summary = cash_portfolio_summary(trades_df, daily_assets_df, skipped_df, args)
    return trades_df, daily_assets_df, skipped_df, summary


def cash_portfolio_summary(
    trades: pd.DataFrame,
    daily_assets: pd.DataFrame,
    skipped: pd.DataFrame,
    args: argparse.Namespace,
) -> pd.DataFrame:
    pnl = numeric_series(trades, "pnl") if not trades.empty else pd.Series(dtype=float)
    returns = numeric_series(trades, "return_pct") if not trades.empty else pd.Series(dtype=float)
    wins = trades["is_win"].fillna(False).astype(bool) if "is_win" in trades else pd.Series(dtype=bool)
    total_asset = numeric_series(daily_assets, "total_asset") if not daily_assets.empty else pd.Series(dtype=float)
    final_asset = float(total_asset.iloc[-1]) if len(total_asset) else float(args.initial_capital)
    running_peak = total_asset.cummax() if len(total_asset) else pd.Series(dtype=float)
    drawdown = (total_asset - running_peak) / running_peak * 100 if len(total_asset) else pd.Series(dtype=float)

    rows = [
        ("initial_capital", float(args.initial_capital)),
        ("final_asset", final_asset),
        ("total_profit", final_asset - float(args.initial_capital)),
        ("total_return_pct", (final_asset - float(args.initial_capital)) / float(args.initial_capital) * 100 if args.initial_capital else 0.0),
        ("lot_size", int(args.lot_size)),
        ("position_sizing", args.position_sizing),
        ("fees", "no_fees"),
        ("trade_count", int(len(trades))),
        ("win_count", int(wins.sum())),
        ("win_rate_pct", wins.mean() * 100 if len(wins) else 0.0),
        ("avg_trade_pnl", pnl.mean()),
        ("median_trade_pnl", pnl.median()),
        ("avg_trade_return_pct", returns.mean()),
        ("median_trade_return_pct", returns.median()),
        ("max_single_profit", pnl.max() if len(pnl) else None),
        ("max_single_loss", pnl.min() if len(pnl) else None),
        ("max_drawdown_pct", drawdown.min() if len(drawdown) else None),
        ("skipped_count", int(len(skipped))),
        ("cash_shortage_count", int((skipped.get("skip_reason", pd.Series(dtype=str)) == "现金不足买1手").sum()) if not skipped.empty else 0),
    ]
    return pd.DataFrame(rows, columns=["项目", "值"])


def default_portfolio_output(args: argparse.Namespace) -> Path:
    start = str(args.start_date).replace("-", "")
    end = str(args.end_date).replace("-", "")
    return PORTFOLIO_ANALYSIS_DIR / f"portfolio_{start}_{end}.xlsx"


def write_portfolio_markdown(output: Path, summary: pd.DataFrame, args: argparse.Namespace) -> None:
    values = dict(zip(summary["项目"], summary["值"]))

    def fmt_pct(key: str) -> str:
        value = values.get(key)
        if pd.isna(value):
            return ""
        return f"{float(value):.2f}%"

    lines = [
        "# 持有策略分析",
        "",
        f"本分析基于本地每日回放快照，时间范围为 {args.start_date} 到 {args.end_date}。",
        "",
        "真实交易口径：",
        "",
        f"- 入选条件：`{portfolio_selection_description(args)}`",
        f"- 买入价：信号日后的第一个交易日开盘价",
        f"- 最多同时持有：`{args.max_positions}` 只",
        f"- 止盈：`+{args.take_profit * 100:.0f}%`",
        f"- 止损：`-{args.stop_loss * 100:.0f}%`",
        f"- 最长持有：`{args.max_hold_days}` 个交易日",
        f"- 重复股票：卖出后间隔 `{args.reentry_cooldown_days}` 个交易日才允许再次买入",
        f"- 同日同时触发止盈/止损：止损优先",
        "",
        "## 回测结果",
        "",
        "| 指标 | 数值 |",
        "|---|---:|",
        f"| 候选信号数 | {int(values.get('candidate_rows', 0))} |",
        f"| 有次日开盘订单数 | {int(values.get('valid_next_open_orders', 0))} |",
        f"| 实际完成交易数 | {int(values.get('trade_count', 0))} |",
        f"| 盈利交易数 | {int(values.get('win_count', 0))} |",
        f"| 胜率 | {fmt_pct('win_rate_pct')} |",
        f"| 止盈交易数 | {int(values.get('take_profit_count', 0))} |",
        f"| 止盈率 | {fmt_pct('take_profit_rate_pct')} |",
        f"| 止损交易数 | {int(values.get('stop_loss_count', 0))} |",
        f"| 到期卖出数 | {int(values.get('time_exit_count', 0))} |",
        f"| 平均单笔收益 | {fmt_pct('avg_return_pct')} |",
        f"| 单笔收益中位数 | {fmt_pct('median_return_pct')} |",
        f"| 平均持有天数 | {float(values.get('avg_holding_days', 0)):.2f} |",
        f"| 错过/跳过信号数 | {int(values.get('skipped_count', 0))} |",
        "",
        "## 结论",
        "",
        "- 这份结果已经考虑了最多两个持仓槽，满仓时后续信号不会被买入。",
        "- 胜率应以这里的实际完成交易为准，不能再用独立信号的后续最高涨幅命中率替代。",
        "- 详细交易、每日持仓和跳过原因见 Excel：",
        "",
        f"`{output}`",
        "",
    ]

    args.report_md.write_text("\n".join(lines), encoding="utf-8")


def simulate_portfolio(args: argparse.Namespace) -> Path:
    daily_rankings, skipped_snapshots = load_daily_rankings(args)
    if daily_rankings.empty:
        raise RuntimeError(f"没有从 {args.daily_dir} 找到 {args.start_date} 到 {args.end_date} 的每日排行快照")

    candidates = select_portfolio_candidates(daily_rankings, args)
    if candidates.empty:
        raise RuntimeError(f"{args.start_date} 到 {args.end_date} 没有符合组合回测条件的信号")

    price_cache: dict[str, pd.DataFrame] = {}
    orders, skipped_before_orders = build_portfolio_orders(candidates, price_cache, args)
    trades, daily_positions, skipped_during_simulation = simulate_portfolio_trades(orders, price_cache, args)
    cash_trades, cash_daily_assets, cash_skipped_during, cash_summary = simulate_cash_portfolio(orders, price_cache, args)

    skipped_parts = [skipped_snapshots, skipped_before_orders, skipped_during_simulation]
    skipped = pd.concat([part for part in skipped_parts if not part.empty], ignore_index=True) if any(not part.empty for part in skipped_parts) else pd.DataFrame()
    cash_skipped_parts = [skipped_snapshots, skipped_before_orders, cash_skipped_during]
    cash_skipped = pd.concat([part for part in cash_skipped_parts if not part.empty], ignore_index=True) if any(not part.empty for part in cash_skipped_parts) else pd.DataFrame()
    cash_summary = cash_portfolio_summary(cash_trades, cash_daily_assets, cash_skipped, args)
    summary = portfolio_summary(trades, daily_positions, candidates, orders, skipped, args)

    output = args.output or default_portfolio_output(args)
    output.parent.mkdir(parents=True, exist_ok=True)

    with pd.ExcelWriter(output) as writer:
        summary.to_excel(writer, index=False, sheet_name="汇总")
        cash_summary.to_excel(writer, index=False, sheet_name="资金汇总")
        cash_trade_timeline(cash_trades).to_excel(writer, index=False, sheet_name="资金交易时间线")
        cash_trades.to_excel(writer, index=False, sheet_name="资金交易明细")
        cash_daily_assets.to_excel(writer, index=False, sheet_name="资金每日资产")
        cash_skipped.to_excel(writer, index=False, sheet_name="资金跳过信号")
        trade_timeline(trades).to_excel(writer, index=False, sheet_name="交易时间线")
        trades.to_excel(writer, index=False, sheet_name="交易明细")
        daily_positions.to_excel(writer, index=False, sheet_name="每日持仓")
        skipped.to_excel(writer, index=False, sheet_name="错过信号")
        candidates.to_excel(writer, index=False, sheet_name="候选信号")
        portfolio_explanation(args).to_excel(writer, index=False, sheet_name="参数说明")

    if args.report_md:
        write_portfolio_markdown(output, summary, args)

    print(f"已生成组合回测: {output.resolve()}")
    print(summary.to_string(index=False, max_colwidth=100))
    if args.report_md:
        print(f"已更新分析报告: {args.report_md.resolve()}")
    return output


def candidate_mask_for_profile(daily_rankings: pd.DataFrame, profile: dict[str, object]) -> pd.Series:
    mask = pd.Series(True, index=daily_rankings.index)
    mask &= numeric_series(daily_rankings, "latest_turn") >= float(profile["min_latest_turn"])
    mask &= numeric_series(daily_rankings, "current_drawdown_pct") <= float(profile["max_current_drawdown"])

    if profile.get("max_rank") is not None:
        mask &= numeric_series(daily_rankings, "rank") <= float(profile["max_rank"])
    if profile.get("min_rank_score") is not None:
        mask &= numeric_series(daily_rankings, "rank_score") >= float(profile["min_rank_score"])
    if profile.get("min_probability_score") is not None:
        mask &= numeric_series(daily_rankings, "probability_score") >= float(profile["min_probability_score"])
    if profile.get("max_latest_pct_chg") is not None:
        mask &= numeric_series(daily_rankings, "latest_pct_chg") <= float(profile["max_latest_pct_chg"])
    if profile.get("min_latest_pct_chg") is not None:
        mask &= numeric_series(daily_rankings, "latest_pct_chg") >= float(profile["min_latest_pct_chg"])
    if profile.get("max_low_to_latest_pct") is not None:
        mask &= numeric_series(daily_rankings, "current_low_to_latest_pct") <= float(profile["max_low_to_latest_pct"])

    return mask


def profile_label(profile: dict[str, object]) -> str:
    parts = [
        f"latest_turn>={profile['min_latest_turn']:g}",
        f"drawdown<={profile['max_current_drawdown']:g}",
    ]
    optional_labels = [
        ("max_rank", "rank<="),
        ("min_rank_score", "rank_score>="),
        ("min_probability_score", "probability_score>="),
        ("max_latest_pct_chg", "latest_pct_chg<="),
        ("min_latest_pct_chg", "latest_pct_chg>="),
        ("max_low_to_latest_pct", "low_to_latest<="),
    ]
    for key, label in optional_labels:
        value = profile.get(key)
        if value is not None:
            parts.append(f"{label}{value:g}")
    return ", ".join(parts)


def default_optimization_profiles() -> list[dict[str, object]]:
    base = {
        "min_latest_turn": 5.0,
        "max_current_drawdown": 29.0,
        "max_rank": None,
        "min_rank_score": None,
        "min_probability_score": None,
        "max_latest_pct_chg": None,
        "min_latest_pct_chg": None,
        "max_low_to_latest_pct": None,
    }
    profiles: list[dict[str, object]] = [base.copy()]

    for min_turn in [8.0, 10.0, 15.0, 20.0]:
        profile = base.copy()
        profile["min_latest_turn"] = min_turn
        profiles.append(profile)

    for max_drawdown in [28.0, 28.5]:
        profile = base.copy()
        profile["max_current_drawdown"] = max_drawdown
        profiles.append(profile)

    for max_rank in [3.0, 5.0, 10.0]:
        profile = base.copy()
        profile["max_rank"] = max_rank
        profiles.append(profile)

    for min_score in [85.0, 90.0]:
        profile = base.copy()
        profile["min_rank_score"] = min_score
        profiles.append(profile)

    for min_probability in [75.0, 80.0]:
        profile = base.copy()
        profile["min_probability_score"] = min_probability
        profiles.append(profile)

    for max_pct_chg in [0.0, -3.0]:
        profile = base.copy()
        profile["max_latest_pct_chg"] = max_pct_chg
        profiles.append(profile)

    for max_low_to_latest in [1.0, 2.0]:
        profile = base.copy()
        profile["max_low_to_latest_pct"] = max_low_to_latest
        profiles.append(profile)

    for updates in [
        {"min_latest_turn": 8.0, "max_latest_pct_chg": 0.0},
        {"min_latest_turn": 10.0, "max_latest_pct_chg": 0.0},
        {"min_probability_score": 75.0, "max_latest_pct_chg": 0.0},
        {"min_probability_score": 75.0, "max_latest_pct_chg": -3.0},
        {"min_probability_score": 75.0, "max_low_to_latest_pct": 2.0},
        {"min_probability_score": 80.0, "max_low_to_latest_pct": 2.0},
    ]:
        profile = base.copy()
        profile.update(updates)
        profiles.append(profile)

    unique_profiles: list[dict[str, object]] = []
    seen: set[tuple[tuple[str, object], ...]] = set()
    for profile in profiles:
        key = tuple(sorted(profile.items()))
        if key in seen:
            continue
        seen.add(key)
        unique_profiles.append(profile)

    return unique_profiles


def select_candidates_for_profile(daily_rankings: pd.DataFrame, profile: dict[str, object], max_daily_candidates: int) -> pd.DataFrame:
    candidates = daily_rankings[candidate_mask_for_profile(daily_rankings, profile)].copy()
    candidates = candidates.sort_values(["signal_date", "rank"])
    if max_daily_candidates > 0:
        candidates = candidates.groupby("signal_date", group_keys=False).head(max_daily_candidates)
    return candidates.reset_index(drop=True)


def optimization_trade_summary(
    trades: pd.DataFrame,
    daily_positions: pd.DataFrame,
    candidates: pd.DataFrame,
    orders: pd.DataFrame,
    skipped: pd.DataFrame,
    profile: dict[str, object],
    take_profit: float,
    stop_loss: float,
    max_hold_days: int,
    args: argparse.Namespace,
) -> dict[str, object]:
    returns = numeric_series(trades, "return_pct") if not trades.empty else pd.Series(dtype=float)
    wins = trades["is_win"].fillna(False).astype(bool) if "is_win" in trades else pd.Series(dtype=bool)
    exit_reason = trades["exit_reason"] if "exit_reason" in trades else pd.Series(dtype=object)
    max_positions = numeric_series(daily_positions, "end_holding_count").max() if not daily_positions.empty else 0

    row: dict[str, object] = {
        "profile": profile_label(profile),
        "start_date": args.start_date,
        "end_date": args.end_date,
        "candidate_rows": int(len(candidates)),
        "valid_next_open_orders": int(len(orders)),
        "trade_count": int(len(trades)),
        "win_count": int(wins.sum()),
        "win_rate_pct": wins.mean() * 100 if len(wins) else 0.0,
        "take_profit_pct": take_profit * 100,
        "stop_loss_pct": stop_loss * 100,
        "max_hold_days": int(max_hold_days),
        "take_profit_count": int((exit_reason == "take_profit").sum()),
        "stop_loss_count": int((exit_reason == "stop_loss").sum()),
        "time_exit_count": int((exit_reason == "time_exit").sum()),
        "avg_return_pct": returns.mean(),
        "median_return_pct": returns.median(),
        "sum_trade_return_pct": returns.sum(),
        "avg_holding_days": numeric_series(trades, "holding_days").mean() if not trades.empty else None,
        "skipped_count": int(len(skipped)),
        "max_observed_positions": int(max_positions) if pd.notna(max_positions) else 0,
    }
    row.update(profile)
    row["meets_target"] = (
        row["trade_count"] >= args.min_trades
        and row["win_rate_pct"] >= args.target_win_rate
        and pd.notna(row["avg_return_pct"])
        and row["avg_return_pct"] > 0
        and row["max_observed_positions"] <= args.max_positions
    )
    return row


def simulate_portfolio_fast_summary(
    orders: pd.DataFrame,
    price_cache: dict[str, pd.DataFrame],
    args: argparse.Namespace,
) -> dict[str, object]:
    if orders.empty:
        return {
            "trade_count": 0,
            "win_count": 0,
            "win_rate_pct": 0.0,
            "take_profit_count": 0,
            "stop_loss_count": 0,
            "time_exit_count": 0,
            "avg_return_pct": None,
            "median_return_pct": None,
            "sum_trade_return_pct": 0.0,
            "avg_holding_days": None,
            "max_observed_positions": 0,
            "skipped_count": 0,
        }

    price_lookup = getattr(args, "_price_lookup", None) or price_lookup_for_codes(price_cache)
    dates = getattr(args, "_trading_dates", None) or portfolio_trading_dates(price_cache, orders, args)
    orders_by_entry = {
        date: [order for _, order in group.iterrows()]
        for date, group in orders.groupby("entry_date")
    }

    holdings: list[dict[str, object]] = []
    cooldown_until: dict[str, pd.Timestamp] = {}
    returns: list[float] = []
    holding_day_values: list[int] = []
    take_profit_count = 0
    stop_loss_count = 0
    time_exit_count = 0
    skipped_count = 0
    max_observed_positions = 0

    for trade_date in dates:
        trade_date_str = trade_date.strftime("%Y-%m-%d")
        daily_buy_count = 0
        for order in orders_by_entry.get(trade_date_str, []):
            code = normalize_code(order["code"])
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
            holdings.append({
                "code": code,
                "entry_date": trade_date,
                "entry_price": entry_price,
                "take_profit_price": entry_price * (1 + args.take_profit),
                "stop_loss_price": entry_price * (1 - args.stop_loss),
                "holding_days": 0,
            })
            daily_buy_count += 1

        remaining: list[dict[str, object]] = []
        for holding in holdings:
            code = str(holding["code"])
            row = price_lookup.get(code, {}).get(trade_date)
            if row is None:
                remaining.append(holding)
                continue

            holding["holding_days"] = int(holding["holding_days"]) + 1
            holding_days = int(holding["holding_days"])
            high = float(row["high"])
            low = float(row["low"])
            close = float(row["close"])

            exit_price: float | None = None
            exit_reason: str | None = None
            if low <= float(holding["stop_loss_price"]):
                exit_price = float(holding["stop_loss_price"])
                exit_reason = "stop_loss"
                stop_loss_count += 1
            elif high >= float(holding["take_profit_price"]):
                exit_price = float(holding["take_profit_price"])
                exit_reason = "take_profit"
                take_profit_count += 1
            elif holding_days >= args.max_hold_days:
                exit_price = close
                exit_reason = "time_exit"
                time_exit_count += 1

            if exit_price is None or exit_reason is None:
                remaining.append(holding)
            else:
                return_pct = (exit_price - float(holding["entry_price"])) / float(holding["entry_price"]) * 100
                returns.append(return_pct)
                holding_day_values.append(holding_days)
                cooldown_until[code] = cooldown_until_date(price_cache[code], trade_date, args.reentry_cooldown_days)

        holdings = remaining
        max_observed_positions = max(max_observed_positions, len(holdings))

    skipped_count += len(holdings)
    return_series = pd.Series(returns, dtype=float)
    holding_day_series = pd.Series(holding_day_values, dtype=float)
    trade_count = int(len(return_series))
    win_count = int((return_series > 0).sum())

    return {
        "trade_count": trade_count,
        "win_count": win_count,
        "win_rate_pct": win_count / trade_count * 100 if trade_count else 0.0,
        "take_profit_count": int(take_profit_count),
        "stop_loss_count": int(stop_loss_count),
        "time_exit_count": int(time_exit_count),
        "avg_return_pct": return_series.mean() if trade_count else None,
        "median_return_pct": return_series.median() if trade_count else None,
        "sum_trade_return_pct": return_series.sum() if trade_count else 0.0,
        "avg_holding_days": holding_day_series.mean() if trade_count else None,
        "max_observed_positions": int(max_observed_positions),
        "skipped_count": int(skipped_count),
    }


def optimization_row_from_fast_summary(
    summary: dict[str, object],
    candidates: pd.DataFrame,
    orders: pd.DataFrame,
    skipped_before_orders: pd.DataFrame,
    profile: dict[str, object],
    take_profit: float,
    stop_loss: float,
    max_hold_days: int,
    args: argparse.Namespace,
) -> dict[str, object]:
    row: dict[str, object] = {
        "profile": profile_label(profile),
        "start_date": args.start_date,
        "end_date": args.end_date,
        "candidate_rows": int(len(candidates)),
        "valid_next_open_orders": int(len(orders)),
        "take_profit_pct": take_profit * 100,
        "stop_loss_pct": stop_loss * 100,
        "max_hold_days": int(max_hold_days),
        "skipped_count": int(summary.get("skipped_count", 0)) + int(len(skipped_before_orders)),
    }
    row.update(summary)
    row.update(profile)
    avg_return = row.get("avg_return_pct")
    row["meets_target"] = (
        int(row["trade_count"]) >= args.min_trades
        and float(row["win_rate_pct"]) >= args.target_win_rate
        and avg_return is not None
        and pd.notna(avg_return)
        and float(avg_return) > 0
        and int(row["max_observed_positions"]) <= args.max_positions
    )
    return row


def default_optimization_output(args: argparse.Namespace) -> Path:
    start = str(args.start_date).replace("-", "")
    end = str(args.end_date).replace("-", "")
    return PORTFOLIO_OPTIMIZATION_DIR / f"portfolio_optimization_{start}_{end}.xlsx"


def optimization_explanation(args: argparse.Namespace) -> pd.DataFrame:
    rows = [
        ("数据来源", f"从 {args.daily_dir} 下的每日 ranking_snapshot.csv 读取，不重新筛选全市场。"),
        ("目标胜率", f"{args.target_win_rate:g}%。"),
        ("最低交易数", f"实际完成交易至少 {args.min_trades} 笔。"),
        ("仓位限制", f"最多同时持有 {args.max_positions} 只；每日最多买入 {args.max_daily_buys} 只。"),
        ("买入价", "信号日后的第一个交易日开盘价。"),
        ("资金回测", f"初始本金 {args.initial_capital:g}，每手 {args.lot_size} 股，使用可用现金尽量买满整手，不扣交易费用。"),
        ("同日触发", "止损优先，使用保守口径。"),
        ("扫描止盈", ", ".join(f"{value * 100:.0f}%" for value in OPTIMIZE_TAKE_PROFITS)),
        ("扫描止损", ", ".join(f"{value * 100:.0f}%" for value in OPTIMIZE_STOP_LOSSES)),
        ("扫描持有期", ", ".join(f"{value}日" for value in OPTIMIZE_MAX_HOLD_DAYS)),
    ]
    return pd.DataFrame(rows, columns=["项目", "说明"])


def write_optimization_markdown(
    output: Path,
    scan: pd.DataFrame,
    qualified: pd.DataFrame,
    best: pd.DataFrame,
    args: argparse.Namespace,
    timeline: pd.DataFrame | None = None,
    cash_summary: pd.DataFrame | None = None,
    cash_timeline: pd.DataFrame | None = None,
) -> None:
    if not qualified.empty:
        chosen = qualified.iloc[0]
        status = f"已找到满足 {args.target_win_rate:g}% 胜率且交易数 >= {args.min_trades} 的组合。"
    else:
        chosen = best.iloc[0] if not best.empty else pd.Series(dtype=object)
        status = f"未找到满足 {args.target_win_rate:g}% 胜率且交易数 >= {args.min_trades} 的组合。"

    def value(name: str, default: object = "") -> object:
        if chosen.empty:
            return default
        item = chosen.get(name, default)
        if pd.isna(item):
            return default
        return item

    lines = [
        "# 组合策略优化分析",
        "",
        f"时间范围：{args.start_date} 到 {args.end_date}",
        "",
        status,
        "",
        "## 推荐/最佳组合",
        "",
        "| 指标 | 数值 |",
        "|---|---:|",
        f"| 过滤条件 | {value('profile')} |",
        f"| 交易数 | {int(value('trade_count', 0))} |",
        f"| 胜率 | {float(value('win_rate_pct', 0)):.2f}% |",
        f"| 止盈 | {float(value('take_profit_pct', 0)):.0f}% |",
        f"| 止损 | {float(value('stop_loss_pct', 0)):.0f}% |",
        f"| 最长持有 | {int(value('max_hold_days', 0))} 日 |",
        f"| 平均单笔收益 | {float(value('avg_return_pct', 0)):.2f}% |",
        f"| 单笔收益中位数 | {float(value('median_return_pct', 0)):.2f}% |",
        f"| 止盈数 | {int(value('take_profit_count', 0))} |",
        f"| 止损数 | {int(value('stop_loss_count', 0))} |",
        f"| 到期卖出数 | {int(value('time_exit_count', 0))} |",
        "",
        "## 资金回测",
        "",
    ]

    if cash_summary is None or cash_summary.empty:
        lines.append("没有可展示的资金回测结果。")
        lines.append("")
    else:
        cash_values = dict(zip(cash_summary["项目"], cash_summary["值"]))

        def cash_value(name: str, default: object = 0) -> object:
            item = cash_values.get(name, default)
            if pd.isna(item):
                return default
            return item

        lines.extend([
            "| 指标 | 数值 |",
            "|---|---:|",
            f"| 初始本金 | {float(cash_value('initial_capital')):.2f} |",
            f"| 最终资产 | {float(cash_value('final_asset')):.2f} |",
            f"| 总收益 | {float(cash_value('total_profit')):.2f} |",
            f"| 总收益率 | {float(cash_value('total_return_pct')):.2f}% |",
            f"| 实际成交次数 | {int(cash_value('trade_count'))} |",
            f"| 盈利次数 | {int(cash_value('win_count'))} |",
            f"| 资金口径胜率 | {float(cash_value('win_rate_pct')):.2f}% |",
            f"| 每笔平均收益金额 | {float(cash_value('avg_trade_pnl')):.2f} |",
            f"| 每笔平均收益率 | {float(cash_value('avg_trade_return_pct')):.2f}% |",
            f"| 最大单笔亏损 | {float(cash_value('max_single_loss')):.2f} |",
            f"| 最大资金回撤 | {float(cash_value('max_drawdown_pct')):.2f}% |",
            f"| 现金不足买1手次数 | {int(cash_value('cash_shortage_count'))} |",
            "",
        ])

    lines.extend([
        "## 交易时间线",
        "",
    ])

    if timeline is None or timeline.empty:
        lines.append("没有可展示的交易记录。")
        lines.append("")
    else:
        preview = timeline.head(20).copy()
        lines.extend([
            "| 找到日期 | 股票代码 | 股票名称 | 买入日期 | 买入价格 | 卖出日期 | 卖出价格 | 卖出原因 | 持有交易日 | 收益率 |",
            "|---|---|---|---|---:|---|---:|---|---:|---:|",
        ])
        for _, row in preview.iterrows():
            lines.append(
                f"| {row['找到日期']} | {row['股票代码']} | {row['股票名称']} | "
                f"{row['买入日期']} | {float(row['买入价格']):.4f} | "
                f"{row['卖出日期']} | {float(row['卖出价格']):.4f} | "
                f"{row['卖出原因']} | {int(row['持有交易日'])} | {float(row['收益率_pct']):.2f}% |"
            )
        if len(timeline) > len(preview):
            lines.append("")
            lines.append(f"这里只展示前 {len(preview)} 笔，完整 {len(timeline)} 笔见 Excel 的 `推荐策略交易时间线`。")
        lines.append("")

    lines.extend([
        "## 资金交易时间线",
        "",
    ])

    if cash_timeline is None or cash_timeline.empty:
        lines.append("没有可展示的资金交易记录。")
        lines.append("")
    else:
        preview = cash_timeline.head(20).copy()
        lines.extend([
            "| 找到日期 | 股票代码 | 股票名称 | 买入日期 | 买入价格 | 买入股数 | 买入金额 | 卖出日期 | 卖出价格 | 单笔盈亏 | 卖出后现金 |",
            "|---|---|---|---|---:|---:|---:|---|---:|---:|---:|",
        ])
        for _, row in preview.iterrows():
            lines.append(
                f"| {row['找到日期']} | {row['股票代码']} | {row['股票名称']} | "
                f"{row['买入日期']} | {float(row['买入价格']):.4f} | {int(row['买入股数'])} | "
                f"{float(row['买入金额']):.2f} | {row['卖出日期']} | {float(row['卖出价格']):.4f} | "
                f"{float(row['单笔盈亏']):.2f} | {float(row['卖出后现金']):.2f} |"
            )
        if len(cash_timeline) > len(preview):
            lines.append("")
            lines.append(f"这里只展示前 {len(preview)} 笔，完整 {len(cash_timeline)} 笔见 Excel 的 `推荐策略资金交易时间线`。")
        lines.append("")

    lines.extend([
        "## 结论",
        "",
        "- 这里的胜率是双仓位真实交易胜率，不是独立信号最高涨幅命中率。",
        "- 若没有达标组合，说明当前模型在这个样本和真实交易约束下不能可靠达到 80%。",
        "- 详细扫描和交易明细见 Excel：",
        "",
        f"`{output}`",
        "",
    ])
    args.report_md.write_text("\n".join(lines), encoding="utf-8")


def optimize_portfolio(args: argparse.Namespace) -> Path:
    daily_rankings, skipped_snapshots = load_daily_rankings(args)
    if daily_rankings.empty:
        raise RuntimeError(f"没有从 {args.daily_dir} 找到 {args.start_date} 到 {args.end_date} 的每日排行快照")

    profiles = default_optimization_profiles()
    price_cache: dict[str, pd.DataFrame] = {}
    scan_rows: list[dict[str, object]] = []
    best_bundle: tuple[dict[str, object], pd.DataFrame, pd.DataFrame, pd.DataFrame, dict[str, object]] | None = None
    qualified_bundle: tuple[dict[str, object], pd.DataFrame, pd.DataFrame, pd.DataFrame, dict[str, object]] | None = None

    for profile_index, profile in enumerate(profiles, start=1):
        candidates = select_candidates_for_profile(daily_rankings, profile, args.max_daily_candidates)
        if candidates.empty:
            continue

        orders, skipped_before_orders = build_portfolio_orders(candidates, price_cache, args)
        if orders.empty:
            continue

        max_hold_for_dates = max(OPTIMIZE_MAX_HOLD_DAYS)
        date_args = argparse.Namespace(**vars(args))
        date_args.max_hold_days = max_hold_for_dates
        shared_args = argparse.Namespace(**vars(args))
        shared_args._price_lookup = price_lookup_for_codes(price_cache)
        shared_args._trading_dates = portfolio_trading_dates(price_cache, orders, date_args)

        if args.progress:
            print(f"扫描画像 {profile_index}/{len(profiles)}: {profile_label(profile)}，候选 {len(candidates)}")

        for take_profit in OPTIMIZE_TAKE_PROFITS:
            for stop_loss in OPTIMIZE_STOP_LOSSES:
                for max_hold_days in OPTIMIZE_MAX_HOLD_DAYS:
                    run_args = argparse.Namespace(**vars(shared_args))
                    run_args.take_profit = take_profit
                    run_args.stop_loss = stop_loss
                    run_args.max_hold_days = max_hold_days
                    run_args.reentry_cooldown_days = args.reentry_cooldown_days

                    fast_summary = simulate_portfolio_fast_summary(orders, price_cache, run_args)
                    row = optimization_row_from_fast_summary(
                        fast_summary,
                        candidates,
                        orders,
                        skipped_before_orders,
                        profile,
                        take_profit,
                        stop_loss,
                        max_hold_days,
                        args,
                    )
                    scan_rows.append(row)

                    bundle = (row, candidates, orders, skipped_before_orders, profile)
                    if row["trade_count"] >= args.min_trades and row["avg_return_pct"] is not None and row["avg_return_pct"] > 0:
                        if best_bundle is None:
                            best_bundle = bundle
                        else:
                            best = best_bundle[0]
                            current_key = (row["win_rate_pct"], row["sum_trade_return_pct"], row["trade_count"])
                            best_key = (best["win_rate_pct"], best["sum_trade_return_pct"], best["trade_count"])
                            if current_key > best_key:
                                best_bundle = bundle

                    if row["meets_target"]:
                        if qualified_bundle is None:
                            qualified_bundle = bundle
                        else:
                            current_key = (row["sum_trade_return_pct"], row["win_rate_pct"], row["trade_count"])
                            qualified_key = (
                                qualified_bundle[0]["sum_trade_return_pct"],
                                qualified_bundle[0]["win_rate_pct"],
                                qualified_bundle[0]["trade_count"],
                            )
                            if current_key > qualified_key:
                                qualified_bundle = bundle

    if not scan_rows:
        raise RuntimeError("没有生成任何优化扫描结果")

    scan = pd.DataFrame(scan_rows)
    scan = scan.sort_values(
        ["meets_target", "win_rate_pct", "sum_trade_return_pct", "trade_count"],
        ascending=[False, False, False, False],
    ).reset_index(drop=True)
    qualified = scan[scan["meets_target"]].copy()
    if not qualified.empty:
        qualified = qualified.sort_values(
            ["sum_trade_return_pct", "win_rate_pct", "trade_count"],
            ascending=[False, False, False],
        ).reset_index(drop=True)
    best = scan[scan["trade_count"] >= args.min_trades].copy()
    best = best.sort_values(["win_rate_pct", "sum_trade_return_pct", "trade_count"], ascending=[False, False, False]).head(50)

    chosen_bundle = qualified_bundle or best_bundle
    chosen_trades = pd.DataFrame()
    chosen_daily_positions = pd.DataFrame()
    chosen_skipped = pd.DataFrame()
    chosen_candidates = pd.DataFrame()
    chosen_cash_trades = pd.DataFrame()
    chosen_cash_daily_assets = pd.DataFrame()
    chosen_cash_skipped = pd.DataFrame()
    chosen_cash_summary = pd.DataFrame()

    if chosen_bundle is not None:
        chosen_row, chosen_candidates, chosen_orders, chosen_skipped_before, _chosen_profile = chosen_bundle
        final_args = argparse.Namespace(**vars(args))
        final_args.take_profit = float(chosen_row["take_profit_pct"]) / 100
        final_args.stop_loss = float(chosen_row["stop_loss_pct"]) / 100
        final_args.max_hold_days = int(chosen_row["max_hold_days"])
        final_args.reentry_cooldown_days = args.reentry_cooldown_days
        chosen_trades, chosen_daily_positions, chosen_skipped_during = simulate_portfolio_trades(chosen_orders, price_cache, final_args)
        chosen_skipped_parts = [skipped_snapshots, chosen_skipped_before, chosen_skipped_during]
        chosen_skipped = pd.concat([part for part in chosen_skipped_parts if not part.empty], ignore_index=True) if any(not part.empty for part in chosen_skipped_parts) else pd.DataFrame()
        chosen_cash_trades, chosen_cash_daily_assets, chosen_cash_skipped_during, _cash_summary = simulate_cash_portfolio(chosen_orders, price_cache, final_args)
        chosen_cash_skipped_parts = [skipped_snapshots, chosen_skipped_before, chosen_cash_skipped_during]
        chosen_cash_skipped = pd.concat([part for part in chosen_cash_skipped_parts if not part.empty], ignore_index=True) if any(not part.empty for part in chosen_cash_skipped_parts) else pd.DataFrame()
        chosen_cash_summary = cash_portfolio_summary(chosen_cash_trades, chosen_cash_daily_assets, chosen_cash_skipped, final_args)

    output = args.output or default_optimization_output(args)
    output.parent.mkdir(parents=True, exist_ok=True)
    chosen_timeline = trade_timeline(chosen_trades)
    chosen_cash_timeline = cash_trade_timeline(chosen_cash_trades)

    with pd.ExcelWriter(output) as writer:
        scan.to_excel(writer, index=False, sheet_name="参数扫描汇总")
        qualified.to_excel(writer, index=False, sheet_name="达标组合")
        best.to_excel(writer, index=False, sheet_name="最佳未达标组合")
        chosen_cash_summary.to_excel(writer, index=False, sheet_name="推荐策略资金汇总")
        chosen_cash_timeline.to_excel(writer, index=False, sheet_name="推荐策略资金交易时间线")
        chosen_cash_trades.to_excel(writer, index=False, sheet_name="推荐策略资金交易明细")
        chosen_cash_daily_assets.to_excel(writer, index=False, sheet_name="推荐策略资金每日资产")
        chosen_cash_skipped.to_excel(writer, index=False, sheet_name="推荐策略资金跳过信号")
        chosen_timeline.to_excel(writer, index=False, sheet_name="推荐策略交易时间线")
        chosen_trades.to_excel(writer, index=False, sheet_name="推荐策略交易明细")
        chosen_daily_positions.to_excel(writer, index=False, sheet_name="推荐策略每日持仓")
        chosen_skipped.to_excel(writer, index=False, sheet_name="推荐策略错过信号")
        chosen_candidates.to_excel(writer, index=False, sheet_name="推荐策略候选信号")
        optimization_explanation(args).to_excel(writer, index=False, sheet_name="参数说明")

    if args.report_md:
        write_optimization_markdown(
            output,
            scan,
            qualified,
            best,
            args,
            chosen_timeline,
            chosen_cash_summary,
            chosen_cash_timeline,
        )

    print(f"已生成组合优化: {output.resolve()}")
    if qualified.empty:
        print(f"未找到胜率 >= {args.target_win_rate:g}% 且交易数 >= {args.min_trades} 的组合。")
        print(best.head(10).to_string(index=False, max_colwidth=100))
    else:
        print(f"找到 {len(qualified)} 个达标组合。")
        print(qualified.head(10).to_string(index=False, max_colwidth=100))
    if args.report_md:
        print(f"已更新优化报告: {args.report_md.resolve()}")
    return output


def optional_path(value: str) -> Path | None:
    return None if value == "" else Path(value)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="保存并验证当前股票筛选记录。")
    subparsers = parser.add_subparsers(dest="command", required=True)

    save_parser = subparsers.add_parser("save", help="保存当前筛选和概率排行快照")
    save_parser.add_argument("--ranking", type=Path, default=RANKING_FILE, help=f"概率排行文件，默认 {RANKING_FILE}")
    save_parser.add_argument("--screening", type=Path, default=SCREENING_FILE, help=f"筛选结果文件，默认 {SCREENING_FILE}")
    save_parser.add_argument("--record-dir", type=Path, default=RECORD_DIR, help=f"记录目录，默认 {RECORD_DIR}")
    save_parser.add_argument("--suffix", default=None, help="可选记录后缀，例如 after_7pct_filter")

    verify_parser = subparsers.add_parser("verify", help="用更新后的 parquet 行情验证历史快照")
    verify_parser.add_argument("--snapshot", type=Path, default=None, help="指定快照目录；默认使用最新快照")
    verify_parser.add_argument("--record-dir", type=Path, default=RECORD_DIR, help=f"记录目录，默认 {RECORD_DIR}")

    verify_all_parser = subparsers.add_parser("verify-all", help="验证所有历史快照")
    verify_all_parser.add_argument("--record-dir", type=Path, default=RECORD_DIR, help=f"记录目录，默认 {RECORD_DIR}")

    summary_parser = subparsers.add_parser("summary", help="汇总所有历史快照的最新验证结果")
    summary_parser.add_argument("--record-dir", type=Path, default=RECORD_DIR, help=f"记录目录，默认 {RECORD_DIR}")
    summary_parser.add_argument("--output", type=Path, default=SUMMARY_FILE, help=f"汇总 Excel，默认 {SUMMARY_FILE}")
    summary_parser.add_argument("--verify-missing", action="store_true", help="没有验证文件的快照先自动验证")

    backfill_parser = subparsers.add_parser("backfill-daily", help="按日期范围回放每日模型结果，并保存到 verification_records/daily_replay")
    backfill_parser.add_argument("--start-date", required=True, help="回放开始日期，例如 2026-01-01")
    backfill_parser.add_argument("--end-date", required=True, help="回放结束日期，例如 2026-05-31")
    backfill_parser.add_argument("--top-n", type=int, default=3, help="打印说明用的 TopN；每日快照仍保存完整排行，默认 3")
    backfill_parser.add_argument("--record-dir", type=Path, default=RECORD_DIR, help=f"记录目录，默认 {RECORD_DIR}")
    backfill_parser.add_argument("--suffix", default="rebound_top3", help="每日快照目录后缀，默认 rebound_top3")
    backfill_parser.add_argument("--trend-threshold", type=float, default=0.07, help="趋势反转阈值，默认 0.07")
    backfill_parser.add_argument("--drawdown-min", type=float, default=0.27, help="回撤下限，默认 0.27")
    backfill_parser.add_argument("--drawdown-max", type=float, default=0.33, help="回撤上限，默认 0.33")
    backfill_parser.add_argument("--bottom-area-max", type=float, default=0.05, help="bottom_area 分界，默认 0.05")
    backfill_parser.add_argument("--current-rebound-max", type=float, default=0.05, help="当日收盘距当前低点最大涨幅，默认 0.05")
    backfill_parser.add_argument("--full-rebound-min", type=float, default=0.20, help="历史有效事件反弹下限，默认 0.20")
    backfill_parser.add_argument("--min-events", type=int, default=1, help="至少出现的历史有效事件次数，默认 1")
    backfill_parser.add_argument("--include-st", action="store_true", help="包含名称中带 ST 的股票；默认排除")
    backfill_parser.add_argument("--workers", type=int, default=None, help="并发回放进程数；默认 CPU 核心数减 1")
    backfill_parser.add_argument("--no-excel-snapshot", action="store_true", help="只保存 CSV 和 manifest，跳过每日 snapshot.xlsx")
    backfill_parser.add_argument("--skip-existing", action="store_true", help="已有 ranking_snapshot.csv 的日期直接跳过")
    backfill_parser.add_argument("--progress", action="store_true", help="打印加载和回放进度")
    backfill_parser.add_argument("--verbose-errors", action="store_true", help="打印单只股票回放错误")

    analyze_parser = subparsers.add_parser("analyze-month", help="从每日回放快照读取信号，按月份或日期范围统计后续涨幅胜率")
    analyze_parser.add_argument("--start-date", required=True, help="分析开始日期，例如 2026-01-01")
    analyze_parser.add_argument("--end-date", required=True, help="分析结束日期，例如 2026-05-31")
    analyze_parser.add_argument("--selection-mode", choices=["top-n", "score-threshold"], default="top-n", help="入选模式：top-n 固定每天前 N 名；score-threshold 按分数门槛入选，默认 top-n")
    analyze_parser.add_argument("--top-n", type=int, default=3, help="每天取排行前 N 名，默认 3")
    analyze_parser.add_argument("--min-rank-score", type=float, default=None, help="score-threshold 模式下 rank_score 入选下限")
    analyze_parser.add_argument("--min-probability-score", type=float, default=None, help="score-threshold 模式下 probability_score 入选下限")
    analyze_parser.add_argument("--lookahead-days", type=int, default=20, help="入选后观察交易日数量，默认 20")
    analyze_parser.add_argument("--success-threshold", type=float, default=0.10, help="成功涨幅阈值，默认 0.10，即超过 10%%")
    analyze_parser.add_argument("--daily-dir", type=Path, default=DAILY_REPLAY_DIR, help=f"每日回放快照目录，默认 {DAILY_REPLAY_DIR}")
    analyze_parser.add_argument("--output", type=Path, default=None, help="输出 Excel；默认写入 monthly_analysis 自动命名文件")
    analyze_parser.add_argument("--dedupe", dest="dedupe", action="store_true", default=True, help="同一股票只保留第一次入选；默认开启")
    analyze_parser.add_argument("--no-dedupe", dest="dedupe", action="store_false", help="不去重，每日 TopN 信号都参与统计")
    analyze_parser.add_argument("--require-full-lookahead", dest="require_full_lookahead", action="store_true", default=True, help="未来不足观察天数的样本不计入主胜率；默认开启")
    analyze_parser.add_argument("--include-short-lookahead", dest="require_full_lookahead", action="store_false", help="未来不足观察天数的样本也计入主胜率")
    analyze_parser.add_argument("--min-latest-turn", type=float, default=1.85, help="过滤A/B使用的最低换手率，默认 1.85")
    analyze_parser.add_argument("--max-latest-pct-chg", type=float, default=0.31, help="过滤A使用的最新日涨幅上限，默认 0.31")
    analyze_parser.add_argument("--min-current-drawdown", type=float, default=28.7, help="过滤B使用的当前回撤下限百分比，默认 28.7")

    portfolio_parser = subparsers.add_parser("simulate-portfolio", help="按真实持仓限制模拟组合交易")
    portfolio_parser.add_argument("--start-date", required=True, help="分析开始日期，例如 2025-01-01")
    portfolio_parser.add_argument("--end-date", required=True, help="分析结束日期，例如 2025-12-31")
    portfolio_parser.add_argument("--selection-mode", choices=["all", "top-n", "score-threshold"], default="all", help="候选模式，默认 all，即先看完整排行再按过滤条件和每日上限取候选")
    portfolio_parser.add_argument("--top-n", type=int, default=2, help="top-n 模式下每天先取排行前 N 名，默认 2")
    portfolio_parser.add_argument("--min-rank-score", type=float, default=None, help="rank_score 入选下限；score-threshold 模式下也参与预筛选")
    portfolio_parser.add_argument("--min-probability-score", type=float, default=None, help="score-threshold 模式下 probability_score 入选下限")
    portfolio_parser.add_argument("--min-latest-turn", type=float, default=5.0, help="最低换手率，默认 5")
    portfolio_parser.add_argument("--min-current-drawdown", type=float, default=None, help="当前回撤百分比下限，例如 28 表示只保留回撤至少 28%% 的信号")
    portfolio_parser.add_argument("--max-current-drawdown", type=float, default=29.0, help="当前回撤百分比上限，默认 29")
    portfolio_parser.add_argument("--max-latest-pct-chg", type=float, default=None, help="最新日涨幅上限百分比，例如 0 表示不追涨")
    portfolio_parser.add_argument("--min-latest-pct-chg", type=float, default=None, help="最新日涨幅下限百分比")
    portfolio_parser.add_argument("--max-low-to-latest-pct", type=float, default=None, help="当前低点到最新收盘涨幅上限百分比")
    portfolio_parser.add_argument("--min-close-drawdown", type=float, default=None, help="按最新收盘价计算的当前回撤下限百分比")
    portfolio_parser.add_argument("--max-close-drawdown", type=float, default=None, help="按最新收盘价计算的当前回撤上限百分比")
    portfolio_parser.add_argument("--min-next-open-to-signal-close-pct", type=float, default=None, help="次日开盘价相对信号日收盘价跌幅下限百分比，例如 -2 表示低开超过 2%% 不买")
    portfolio_parser.add_argument("--max-next-open-to-signal-close-pct", type=float, default=None, help="次日开盘价相对信号日收盘价涨幅上限百分比，例如 2 表示高开超过 2%% 不买")
    portfolio_parser.add_argument("--max-next-open-to-current-low-pct", type=float, default=None, help="次日开盘价相对当前低点涨幅上限百分比，例如 5 表示开盘已离低点超过 5%% 不买")
    portfolio_parser.add_argument("--max-rank", type=float, default=None, help="最大排行名次，例如 10")
    portfolio_parser.add_argument("--min-daily-candidates", type=int, default=None, help="每天通过过滤的候选数下限，例如 2 表示市场共振才交易")
    portfolio_parser.add_argument("--max-daily-candidates", type=int, default=2, help="每天最多候选数，默认 2")
    portfolio_parser.add_argument("--max-positions", type=int, default=2, help="最多同时持仓数，默认 2")
    portfolio_parser.add_argument("--max-daily-buys", type=int, default=2, help="每天最多新买入数量，默认 2")
    portfolio_parser.add_argument("--take-profit", type=float, default=0.05, help="止盈比例，默认 0.05")
    portfolio_parser.add_argument("--stop-loss", type=float, default=0.03, help="止损比例，默认 0.03")
    portfolio_parser.add_argument("--max-hold-days", type=int, default=10, help="最多持有交易日，默认 10")
    portfolio_parser.add_argument("--reentry-cooldown-days", type=int, default=10, help="同一股票卖出后重新买入冷却交易日，默认 10")
    portfolio_parser.add_argument("--initial-capital", type=float, default=10000.0, help="资金回测初始本金，默认 10000")
    portfolio_parser.add_argument("--lot-size", type=int, default=100, help="每手股数，默认 100")
    portfolio_parser.add_argument("--position-sizing", choices=["all-cash"], default="all-cash", help="资金回测买入方式：all-cash 表示用可用现金尽量买满整手")
    portfolio_parser.add_argument("--daily-dir", type=Path, default=DAILY_REPLAY_DIR, help=f"每日回放快照目录，默认 {DAILY_REPLAY_DIR}")
    portfolio_parser.add_argument("--snapshot-suffix", default=None, help="只读取指定 daily_replay 后缀的快照，例如 rebound_f10")
    portfolio_parser.add_argument("--output", type=Path, default=None, help="输出 Excel；默认写入 portfolio_analysis 自动命名文件")
    portfolio_parser.add_argument("--report-md", type=optional_path, default=Path("holding_strategy_analysis.md"), help="同步更新 Markdown 报告；传空字符串可关闭")

    optimize_parser = subparsers.add_parser("optimize-portfolio", help="扫描选股过滤和卖出规则，寻找满足目标胜率的组合策略")
    optimize_parser.add_argument("--start-date", required=True, help="优化开始日期，例如 2025-01-01")
    optimize_parser.add_argument("--end-date", required=True, help="优化结束日期，例如 2025-12-31")
    optimize_parser.add_argument("--daily-dir", type=Path, default=DAILY_REPLAY_DIR, help=f"每日回放快照目录，默认 {DAILY_REPLAY_DIR}")
    optimize_parser.add_argument("--target-win-rate", type=float, default=80.0, help="目标胜率百分比，默认 80")
    optimize_parser.add_argument("--min-trades", type=int, default=30, help="最低实际完成交易数，默认 30")
    optimize_parser.add_argument("--max-daily-candidates", type=int, default=2, help="每天最多候选数，默认 2")
    optimize_parser.add_argument("--max-positions", type=int, default=2, help="最多同时持仓数，默认 2")
    optimize_parser.add_argument("--max-daily-buys", type=int, default=2, help="每天最多新买入数量，默认 2")
    optimize_parser.add_argument("--reentry-cooldown-days", type=int, default=10, help="同一股票卖出后重新买入冷却交易日，默认 10")
    optimize_parser.add_argument("--initial-capital", type=float, default=10000.0, help="资金回测初始本金，默认 10000")
    optimize_parser.add_argument("--lot-size", type=int, default=100, help="每手股数，默认 100")
    optimize_parser.add_argument("--position-sizing", choices=["all-cash"], default="all-cash", help="资金回测买入方式：all-cash 表示用可用现金尽量买满整手")
    optimize_parser.add_argument("--output", type=Path, default=None, help="输出 Excel；默认写入 portfolio_analysis/optimization 自动命名文件")
    optimize_parser.add_argument("--report-md", type=Path, default=Path("holding_strategy_analysis.md"), help="同步更新 Markdown 报告；传空字符串可关闭")
    optimize_parser.add_argument("--progress", action="store_true", help="打印优化扫描进度")

    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.command == "save":
        save_snapshot(args)
    elif args.command == "verify":
        verify_snapshot(args)
    elif args.command == "verify-all":
        verify_all_snapshots(args)
    elif args.command == "summary":
        summary_all_snapshots(args)
    elif args.command == "backfill-daily":
        backfill_daily_snapshots(args)
    elif args.command == "analyze-month":
        analyze_month(args)
    elif args.command == "simulate-portfolio":
        if args.report_md == Path(""):
            args.report_md = None
        simulate_portfolio(args)
    elif args.command == "optimize-portfolio":
        if args.report_md == Path(""):
            args.report_md = None
        optimize_portfolio(args)


if __name__ == "__main__":
    main()
