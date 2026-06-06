from __future__ import annotations

import re
import sys
from pathlib import Path

import pandas as pd


BASE_DIR = Path("a_stock_data")
PARQUET_DIR = BASE_DIR / "parquet"
EXCEL_DIR = BASE_DIR / "excel_export"
META_DIR = BASE_DIR / "meta"

STOCK_LIST_FILE = META_DIR / "stock_list.csv"


def normalize_code(code) -> str:
    code = str(code).strip()
    if "." in code:
        code = code.split(".")[-1]
    return code.zfill(6)


def safe_filename(name: str) -> str:
    return re.sub(r'[\\/:*?"<>|]', "_", str(name)).strip()


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


def export_stock_excel(code: str) -> Path:
    code = normalize_code(code)

    parquet_path = PARQUET_DIR / f"{code}.parquet"

    if not parquet_path.exists():
        raise FileNotFoundError(f"没有找到数据文件: {parquet_path}")

    name_map = load_stock_name_map()
    name = name_map.get(code, code)

    df = pd.read_parquet(parquet_path)

    if df.empty:
        raise RuntimeError(f"数据为空: {parquet_path}")

    out = df.copy()

    if "date" in out.columns:
        out["date"] = pd.to_datetime(out["date"], errors="coerce").dt.strftime("%Y-%m-%d")

    # 常用字段排前面
    preferred_cols = [
        "date",
        "code",
        "bs_code",
        "open",
        "high",
        "low",
        "close",
        "preclose",
        "volume",
        "amount",
        "adjustflag",
        "turn",
        "tradestatus",
        "pctChg",
        "pct_chg",
        "peTTM",
        "pbMRQ",
        "psTTM",
        "pcfNcfTTM",
        "isST",
    ]

    cols = [c for c in preferred_cols if c in out.columns]
    others = [c for c in out.columns if c not in cols]
    out = out[cols + others]

    EXCEL_DIR.mkdir(parents=True, exist_ok=True)

    out_path = EXCEL_DIR / f"{code}_{safe_filename(name)}.xlsx"
    out.to_excel(out_path, index=False, sheet_name="daily")

    return out_path


def main() -> None:
    if len(sys.argv) < 2:
        print("用法:")
        print("  python export_stock_excel.py 000001")
        print("  python export_stock_excel.py 600519")
        sys.exit(1)

    code = sys.argv[1]

    out_path = export_stock_excel(code)

    print(f"已导出: {out_path.resolve()}")


if __name__ == "__main__":
    main()
