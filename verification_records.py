from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path

import pandas as pd


BASE_DIR = Path("a_stock_data")
PARQUET_DIR = BASE_DIR / "parquet"
RECORD_DIR = BASE_DIR / "verification_records"

SCREENING_FILE = Path("drawdown_rebound_history_result.xlsx")
RANKING_FILE = Path("tomorrow_rebound_probability_ranking.xlsx")
SUMMARY_FILE = RECORD_DIR / "verification_summary.xlsx"
DEFAULT_HIT_THRESHOLDS = [3.0, 5.0, 10.0, 20.0]


def normalize_code(code) -> str:
    code = str(code).strip()
    if "." in code:
        code = code.split(".")[-1]
    return code.zfill(6)


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

    snapshots = [path for path in record_dir.iterdir() if path.is_dir()]
    if not snapshots:
        raise FileNotFoundError(f"没有任何记录快照: {record_dir}")

    return sorted(snapshots)[-1]


def snapshot_dirs(record_dir: Path) -> list[Path]:
    if not record_dir.exists():
        return []

    return sorted(path for path in record_dir.iterdir() if path.is_dir())


def numeric_series(df: pd.DataFrame, column: str) -> pd.Series:
    if column not in df.columns:
        return pd.Series(dtype=float)
    return pd.to_numeric(df[column], errors="coerce")


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


if __name__ == "__main__":
    main()
