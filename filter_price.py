from __future__ import annotations

from pathlib import Path

import pandas as pd


BASE_DIR = Path("a_stock_data")
PARQUET_DIR = BASE_DIR / "parquet"
META_DIR = BASE_DIR / "meta"

STOCK_LIST_FILE = META_DIR / "stock_list.csv"
OUT_FILE = Path("price_filter_result.xlsx")


def normalize_code(code) -> str:
    code = str(code).strip()
    if "." in code:
        code = code.split(".")[-1]
    return code.zfill(6)


def load_stock_name_map() -> dict[str, str]:
    if not STOCK_LIST_FILE.exists():
        return {}

    stock_list = pd.read_csv(STOCK_LIST_FILE, dtype=str)

    # 兼容不同脚本生成的字段
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


def get_latest_row(path: Path):
    df = pd.read_parquet(path)

    if df.empty:
        return None

    if "date" not in df.columns or "close" not in df.columns:
        print(f"字段异常，跳过: {path}, columns={list(df.columns)}")
        return None

    df = df.copy()
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df["close"] = pd.to_numeric(df["close"], errors="coerce")

    df = df.dropna(subset=["date", "close"])

    if df.empty:
        return None

    return df.sort_values("date").iloc[-1]


def main() -> None:
    if not PARQUET_DIR.exists():
        raise FileNotFoundError(f"没有找到数据目录: {PARQUET_DIR}")

    name_map = load_stock_name_map()

    rows = []

    parquet_files = sorted(PARQUET_DIR.glob("*.parquet"))

    if not parquet_files:
        print(f"没有找到 parquet 文件: {PARQUET_DIR}")
        return

    for path in parquet_files:
        code = normalize_code(path.stem)

        try:
            latest = get_latest_row(path)
        except Exception as e:
            print(f"读取失败: {path}, {type(e).__name__}: {e}")
            continue

        if latest is None:
            continue

        close = float(latest["close"])

        if (40 <= close <= 45) or (90 <= close < 100):
            rows.append({
                "code": code,
                "name": name_map.get(code, ""),
                "date": pd.to_datetime(latest["date"]).strftime("%Y-%m-%d"),
                "close": close,
                "open": latest.get("open"),
                "high": latest.get("high"),
                "low": latest.get("low"),
                "preclose": latest.get("preclose"),
                "pct_chg": latest.get("pct_chg", latest.get("pctChg")),
                "volume": latest.get("volume"),
                "amount": latest.get("amount"),
                "turn": latest.get("turn"),
                "tradestatus": latest.get("tradestatus"),
                "isST": latest.get("isST"),
            })

    result = pd.DataFrame(rows)

    if result.empty:
        print("没有筛选到符合条件的股票")
        return

    result = result.sort_values(["close", "code"]).reset_index(drop=True)

    print(result.to_string(index=False))

    result.to_excel(OUT_FILE, index=False)
    print()
    print(f"已导出: {OUT_FILE.resolve()}")
    print(f"共 {len(result)} 只")


if __name__ == "__main__":
    main()
