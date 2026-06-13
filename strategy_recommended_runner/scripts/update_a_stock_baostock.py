from __future__ import annotations

import sys
import time
import random
import signal
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path

import baostock as bs
import pandas as pd
from tqdm import tqdm


# ============================================================
# 配置区
# ============================================================

START_DATE = "2016-01-01"

BASE_DIR = Path("a_stock_data")
PARQUET_DIR = BASE_DIR / "parquet"
META_DIR = BASE_DIR / "meta"
LOG_DIR = BASE_DIR / "logs"

STOCK_LIST_FILE = META_DIR / "stock_list.csv"
FAILED_FILE = LOG_DIR / "failed.csv"
REQUEST_STATE_FILE = META_DIR / "request_state.csv"
REQUEST_LOG_FILE = LOG_DIR / "request_log.csv"

# Baostock 复权标志：
# "1" = 后复权
# "2" = 前复权
# "3" = 不复权
ADJUST_FLAG = "3"

SLEEP_MIN = 0.3
SLEEP_MAX = 0.8

MAX_RETRY = 3
QUERY_TIMEOUT_SECONDS = 60
RECONNECT_INTERVAL = 200
MAX_RECONNECT_RETRY = 5


# ============================================================
# 基础工具
# ============================================================

def ensure_dirs() -> None:
    for path in [PARQUET_DIR, META_DIR, LOG_DIR]:
        path.mkdir(parents=True, exist_ok=True)


def today_date_str() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def normalize_code(code: str) -> str:
    """
    sh.600519 -> 600519
    sz.000001 -> 000001
    000001    -> 000001
    """
    code = str(code).strip()
    if "." in code:
        code = code.split(".")[-1]
    return code.zfill(6)


def to_baostock_code(code: str) -> str:
    """
    Baostock 代码格式：
    600519 -> sh.600519
    688981 -> sh.688981
    000001 -> sz.000001
    300750 -> sz.300750

    注意：Baostock 主要覆盖沪深市场。北交所支持情况需要实际验证。
    """
    code = normalize_code(code)

    if code.startswith(("6", "9")):
        return f"sh.{code}"

    return f"sz.{code}"


def code_to_parquet_path(code: str) -> Path:
    return PARQUET_DIR / f"{normalize_code(code)}.parquet"


@contextmanager
def query_timeout(seconds: int):
    if seconds <= 0 or not hasattr(signal, "SIGALRM"):
        yield
        return

    def handler(signum, frame):
        raise TimeoutError(f"query timeout after {seconds}s")

    old_handler = signal.signal(signal.SIGALRM, handler)
    signal.alarm(seconds)
    try:
        yield
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, old_handler)


def append_failed(code: str, name: str, reason: str) -> None:
    row = pd.DataFrame([{
        "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "code": normalize_code(code),
        "name": str(name),
        "reason": str(reason)[:1000],
    }])

    if FAILED_FILE.exists():
        old = pd.read_csv(FAILED_FILE, dtype={"code": str})
        out = pd.concat([old, row], ignore_index=True)
    else:
        out = row

    out.to_csv(FAILED_FILE, index=False, encoding="utf-8-sig")


def append_request_log(
    code: str,
    bs_code: str,
    name: str,
    start_date: str,
    end_date: str,
    status: str,
    rows: int = 0,
    reason: str = "",
) -> None:
    row = pd.DataFrame([{
        "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "code": normalize_code(code),
        "bs_code": str(bs_code),
        "name": str(name),
        "start_date": str(start_date),
        "end_date": str(end_date),
        "status": str(status),
        "rows": int(rows),
        "reason": str(reason)[:1000],
    }])

    file_exists = REQUEST_LOG_FILE.exists()
    encoding = "utf-8-sig" if not file_exists else "utf-8"
    row.to_csv(
        REQUEST_LOG_FILE,
        mode="a",
        header=not file_exists,
        index=False,
        encoding=encoding,
    )


def load_request_state() -> dict[str, str]:
    if not REQUEST_STATE_FILE.exists():
        return {}

    df = pd.read_csv(REQUEST_STATE_FILE, dtype=str)
    if df.empty or "code" not in df.columns or "requested_until" not in df.columns:
        return {}

    df = df.copy()
    df["code"] = df["code"].apply(normalize_code)
    df["requested_until"] = df["requested_until"].astype(str)
    df = df.dropna(subset=["code", "requested_until"])
    df = df[df["requested_until"].str.match(r"^\d{4}-\d{2}-\d{2}$", na=False)]
    df = df.sort_values(["code", "requested_until"]).drop_duplicates(subset=["code"], keep="last")

    return dict(zip(df["code"], df["requested_until"]))


def save_request_state(request_state: dict[str, str], stock_info: dict[str, dict[str, str]]) -> None:
    # 先读磁盘现有数据，再合并——内存数据优先，防止覆盖其他股票的记录
    existing: dict[str, str] = {}
    if REQUEST_STATE_FILE.exists():
        try:
            ex_df = pd.read_csv(REQUEST_STATE_FILE, dtype=str)
            if not ex_df.empty and "code" in ex_df.columns and "requested_until" in ex_df.columns:
                ex_df["code"] = ex_df["code"].apply(normalize_code)
                existing = dict(zip(ex_df["code"], ex_df["requested_until"]))
        except Exception:
            pass

    merged = {**existing, **request_state}

    rows = []
    for code, requested_until in sorted(merged.items()):
        info = stock_info.get(code, {})
        rows.append({
            "code": code,
            "bs_code": info.get("bs_code", to_baostock_code(code)),
            "name": info.get("name", ""),
            "requested_until": requested_until,
            "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        })

    out = pd.DataFrame(rows, columns=[
        "code",
        "bs_code",
        "name",
        "requested_until",
        "updated_at",
    ])
    out.to_csv(REQUEST_STATE_FILE, index=False, encoding="utf-8-sig")


def advance_request_state(
    request_state: dict[str, str],
    stock_info: dict[str, dict[str, str]],
    code: str,
    bs_code: str,
    name: str,
    requested_until: str,
    save_to_disk: bool = True,
) -> None:
    code = normalize_code(code)
    old_date = request_state.get(code)

    if old_date is None or requested_until > old_date:
        request_state[code] = requested_until

    stock_info[code] = {"bs_code": str(bs_code), "name": str(name)}
    if save_to_disk:
        save_request_state(request_state, stock_info)


def baostock_result_to_df(rs) -> pd.DataFrame:
    rows = []

    while rs.error_code == "0" and rs.next():
        rows.append(rs.get_row_data())

    if not rows:
        return pd.DataFrame(columns=rs.fields)

    return pd.DataFrame(rows, columns=rs.fields)


# ============================================================
# Baostock 登录 / 登出
# ============================================================

def login_baostock() -> None:
    lg = bs.login()

    if lg.error_code != "0":
        raise RuntimeError(f"baostock login failed: {lg.error_code}, {lg.error_msg}")

    print("Baostock 登录成功")


def logout_baostock() -> None:
    bs.logout()


def reconnect_baostock(max_retry: int = MAX_RECONNECT_RETRY) -> None:
    """登出后重新登录，遇到失败自动指数退避重试"""
    for attempt in range(max_retry):
        try:
            bs.logout()
        except Exception:
            pass
        time.sleep(2.0 + attempt * 2.0)
        try:
            lg = bs.login()
            if lg.error_code == "0":
                print(f"\n[重连成功] 第 {attempt + 1} 次尝试")
                return
            print(f"\n[重连失败] 第 {attempt + 1} 次: {lg.error_code} {lg.error_msg}")
        except Exception as e:
            print(f"\n[重连异常] 第 {attempt + 1} 次: {e}")
    raise RuntimeError(f"baostock 重连失败，已尝试 {max_retry} 次")


# ============================================================
# 股票列表
# ============================================================

def get_stock_list() -> pd.DataFrame:
    """
    获取当前正常上市股票列表。

    query_stock_basic 返回字段常见包括：
    code, code_name, ipoDate, outDate, type, status

    type:
      1 = 股票
      2 = 指数
      3 = 其他

    status:
      1 = 上市
      0 = 退市
    """
    rs = bs.query_stock_basic()

    if rs.error_code != "0":
        raise RuntimeError(f"query_stock_basic failed: {rs.error_code}, {rs.error_msg}")

    df = baostock_result_to_df(rs)

    if df.empty:
        raise RuntimeError("stock list empty")

    required = {"code", "code_name", "type", "status"}
    missing = required - set(df.columns)
    if missing:
        raise RuntimeError(f"stock list missing columns: {missing}, columns={list(df.columns)}")

    df = df.copy()

    # 只保留股票、正常上市
    df = df[(df["type"] == "1") & (df["status"] == "1")]

    df["bs_code"] = df["code"].astype(str)
    df["code"] = df["bs_code"].apply(normalize_code)
    df["name"] = df["code_name"].astype(str)

    # 只保留 6 位数字代码
    df = df[df["code"].str.match(r"^\d{6}$", na=False)]

    df = df[[
        "code",
        "bs_code",
        "name",
        "ipoDate",
        "outDate",
        "type",
        "status",
    ]].copy()

    df = df.drop_duplicates(subset=["code"])
    df = df.sort_values("code").reset_index(drop=True)

    df.to_csv(STOCK_LIST_FILE, index=False, encoding="utf-8-sig")

    return df[["code", "bs_code", "name"]].copy()


# ============================================================
# 历史行情
# ============================================================

def fetch_daily_history(
    bs_code: str,
    start_date: str,
    end_date: str,
) -> pd.DataFrame:
    """
    获取单只股票历史日线。
    """
    fields = ",".join([
        "date",
        "code",
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
        "peTTM",
        "pbMRQ",
        "psTTM",
        "pcfNcfTTM",
        "isST",
    ])

    rs = bs.query_history_k_data_plus(
        code=bs_code,
        fields=fields,
        start_date=start_date,
        end_date=end_date,
        frequency="d",
        adjustflag=ADJUST_FLAG,
    )

    if rs.error_code != "0":
        raise RuntimeError(
            f"query_history_k_data_plus failed: "
            f"code={bs_code}, error={rs.error_code}, msg={rs.error_msg}"
        )

    df = baostock_result_to_df(rs)

    if df.empty:
        return df

    df = df.copy()

    # 统一字段
    df["bs_code"] = df["code"].astype(str)
    df["code"] = df["bs_code"].apply(normalize_code)
    df["date"] = pd.to_datetime(df["date"])

    numeric_cols = [
        "open",
        "high",
        "low",
        "close",
        "preclose",
        "volume",
        "amount",
        "turn",
        "pctChg",
        "peTTM",
        "pbMRQ",
        "psTTM",
        "pcfNcfTTM",
    ]

    for col in numeric_cols:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    int_like_cols = [
        "adjustflag",
        "tradestatus",
        "isST",
    ]

    for col in int_like_cols:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    # Baostock 字段 pctChg 是涨跌幅，保留原字段名，同时加一个更统一的 pct_chg
    if "pctChg" in df.columns:
        df["pct_chg"] = df["pctChg"]

    # 统一排序字段
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

    cols = [c for c in preferred_cols if c in df.columns]
    others = [c for c in df.columns if c not in cols]
    df = df[cols + others]

    df = df.sort_values("date").reset_index(drop=True)
    return df


# ============================================================
# 本地 Parquet 存储
# ============================================================

def read_stock_data(code: str) -> pd.DataFrame:
    path = code_to_parquet_path(code)

    if not path.exists():
        return pd.DataFrame()

    df = pd.read_parquet(path)

    if df.empty:
        return df

    df["date"] = pd.to_datetime(df["date"])
    df["code"] = df["code"].astype(str).str.zfill(6)

    if "bs_code" not in df.columns:
        df["bs_code"] = df["code"].apply(to_baostock_code)

    return df


def write_stock_data(code: str, df: pd.DataFrame) -> None:
    if df.empty:
        return

    path = code_to_parquet_path(code)

    out = df.copy()
    out["date"] = pd.to_datetime(out["date"])
    out["code"] = out["code"].astype(str).str.zfill(6)

    if "bs_code" not in out.columns:
        out["bs_code"] = out["code"].apply(to_baostock_code)

    out = out.drop_duplicates(subset=["code", "date"], keep="last")
    out = out.sort_values(["code", "date"]).reset_index(drop=True)

    path.parent.mkdir(parents=True, exist_ok=True)
    out.to_parquet(path, index=False)


def rebuild_request_state_from_parquet(request_state: dict[str, str]) -> int:
    """扫描 parquet 目录，为 request_state 中缺失的股票补充最新日期，返回补充数量。"""
    filled = 0
    for parquet_file in sorted(PARQUET_DIR.glob("*.parquet")):
        code = parquet_file.stem
        if code in request_state:
            continue
        try:
            df = pd.read_parquet(parquet_file, columns=["date"])
            if df.empty:
                continue
            max_date = pd.to_datetime(df["date"]).max().strftime("%Y-%m-%d")
            request_state[code] = max_date
            filled += 1
        except Exception:
            pass
    return filled


def next_day_str(date_str: str) -> str:
    return (pd.to_datetime(date_str) + timedelta(days=1)).strftime("%Y-%m-%d")


def get_next_start_date(old_df: pd.DataFrame, requested_until: str | None = None) -> str:
    start_dates = [START_DATE]

    if not old_df.empty:
        last_date = pd.to_datetime(old_df["date"]).max()
        start_dates.append((last_date + timedelta(days=1)).strftime("%Y-%m-%d"))

    if requested_until:
        try:
            start_dates.append(next_day_str(requested_until))
        except Exception:
            pass

    return max(start_dates)


def merge_stock_data(old_df: pd.DataFrame, new_df: pd.DataFrame) -> pd.DataFrame:
    if old_df.empty:
        merged = new_df.copy()
    elif new_df.empty:
        merged = old_df.copy()
    else:
        merged = pd.concat([old_df, new_df], ignore_index=True)

    if merged.empty:
        return merged

    merged["date"] = pd.to_datetime(merged["date"])
    merged["code"] = merged["code"].astype(str).str.zfill(6)

    merged = merged.drop_duplicates(subset=["code", "date"], keep="last")
    merged = merged.sort_values(["code", "date"]).reset_index(drop=True)

    return merged


# ============================================================
# 更新逻辑
# ============================================================

def update_one_stock(
    code: str,
    bs_code: str,
    name: str,
    end_date: str,
    request_state: dict[str, str],
    stock_info: dict[str, dict[str, str]],
) -> tuple[str, int]:
    code_norm = normalize_code(code)
    requested_until = request_state.get(code_norm)
    # 快速跳过：request_state 已记录到 end_date，无需读 parquet
    if requested_until and requested_until >= end_date:
        return "skip", 0

    old_df = read_stock_data(code)
    start_date = get_next_start_date(old_df, requested_until)

    if start_date > end_date:
        return "skip", 0

    # 先记录请求进度并立即落盘（合并写入，不会覆盖其他股票记录）。
    advance_request_state(request_state, stock_info, code, bs_code, name, end_date)
    append_request_log(code, bs_code, name, start_date, end_date, "started")

    new_df = pd.DataFrame()
    last_error = None

    for i in range(MAX_RETRY):
        try:
            with query_timeout(QUERY_TIMEOUT_SECONDS):
                new_df = fetch_daily_history(bs_code, start_date, end_date)
            last_error = None
            break
        except Exception as e:
            last_error = e
            append_request_log(
                code,
                bs_code,
                name,
                start_date,
                end_date,
                f"retry_{i + 1}_failed",
                reason=f"{type(e).__name__}: {e}",
            )
            print(f"\n[{code}] 第 {i + 1} 次失败: {e}，正在重连...")
            try:
                reconnect_baostock()
            except RuntimeError as re:
                append_request_log(
                    code, bs_code, name, start_date, end_date,
                    "failed", reason=f"reconnect failed: {re}",
                )
                raise
            time.sleep(2.0 * (i + 1))

    if last_error is not None:
        append_request_log(
            code,
            bs_code,
            name,
            start_date,
            end_date,
            "failed",
            reason=f"{type(last_error).__name__}: {last_error}",
        )
        raise last_error

    if new_df.empty:
        append_request_log(code, bs_code, name, start_date, end_date, "empty")
        return "empty", 0

    merged = merge_stock_data(old_df, new_df)
    write_stock_data(code, merged)

    append_request_log(code, bs_code, name, start_date, end_date, "updated", rows=len(new_df))
    return "updated", len(new_df)


def main() -> None:
    ensure_dirs()

    end_date = today_date_str()

    print("开始更新 A 股日线数据 - Baostock")
    print(f"历史兜底起始日期: {START_DATE}（每支股票按需增量更新，实际从上次记录的日期继续）")
    print(f"结束日期: {end_date}")
    print(f"主存储: {PARQUET_DIR.resolve()}")
    print(f"复权类型: {ADJUST_FLAG}，1=后复权，2=前复权，3=不复权")

    login_baostock()

    try:
        stock_list = get_stock_list()
        print(f"股票数量: {len(stock_list)}")
        print(f"请求进度: {REQUEST_STATE_FILE.resolve()}")
        print(f"请求日志: {REQUEST_LOG_FILE.resolve()}")

        request_state = load_request_state()
        filled = rebuild_request_state_from_parquet(request_state)
        if filled > 0:
            print(f"从 parquet 补充了 {filled} 只股票的进度记录（request_state 有缺失）")
        stock_info = {
            normalize_code(row["code"]): {
                "bs_code": str(row["bs_code"]),
                "name": str(row["name"]),
            }
            for _, row in stock_list.iterrows()
        }

        updated_count = 0
        updated_rows = 0
        skip_count = 0
        empty_count = 0
        failed_count = 0

        for idx, (_, row) in enumerate(tqdm(stock_list.iterrows(), total=len(stock_list))):
            if idx > 0 and idx % RECONNECT_INTERVAL == 0:
                print(f"\n[定期重连] 已处理 {idx} 只，主动刷新 session...")
                save_request_state(request_state, stock_info)
                reconnect_baostock()

            code = normalize_code(row["code"])
            bs_code = str(row["bs_code"])
            name = str(row["name"])

            status = "failed"
            try:
                status, rows = update_one_stock(
                    code,
                    bs_code,
                    name,
                    end_date,
                    request_state,
                    stock_info,
                )

                if status == "updated":
                    updated_count += 1
                    updated_rows += rows
                elif status == "skip":
                    skip_count += 1
                elif status == "empty":
                    empty_count += 1

            except Exception as e:
                failed_count += 1
                append_failed(code, name, f"{type(e).__name__}: {e}")

            if status != "skip":
                time.sleep(random.uniform(SLEEP_MIN, SLEEP_MAX))

        save_request_state(request_state, stock_info)
        print()
        print("更新完成")
        print(f"更新股票数: {updated_count}")
        print(f"新增记录数: {updated_rows}")
        print(f"跳过股票数: {skip_count}")
        print(f"空数据股票数: {empty_count}")
        print(f"失败股票数: {failed_count}")
        print(f"失败日志: {FAILED_FILE.resolve()}")

    finally:
        logout_baostock()


if __name__ == "__main__":
    main()
