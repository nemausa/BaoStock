from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
from pytdx.hq import TdxHq_API
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

WORKERS = 8          # 并发线程数
BARS_PER_CALL = 800  # pytdx 单次最多 800 条

TDX_SERVERS = [
    ("60.12.136.250", 7709),
    ("218.75.126.9", 7709),
    ("115.238.90.165", 7709),
    ("115.238.56.198", 7709),
]

_thread_local = threading.local()
_write_lock = threading.Lock()   # parquet / CSV 文件写入锁
_state_lock = threading.Lock()   # request_state dict + request_state.csv 专用锁


# ============================================================
# TDX 连接管理（每线程独立连接）
# ============================================================

def _make_api() -> TdxHq_API:
    api = TdxHq_API(heartbeat=False, auto_retry=False)
    for host, port in TDX_SERVERS:
        try:
            if api.connect(host, port):
                return api
        except Exception:
            pass
    raise RuntimeError("所有 TDX 服务器均无法连接")


def get_api() -> TdxHq_API:
    if not getattr(_thread_local, "api", None):
        _thread_local.api = _make_api()
    return _thread_local.api


def reset_api() -> TdxHq_API:
    try:
        if _thread_local.api:
            _thread_local.api.disconnect()
    except Exception:
        pass
    _thread_local.api = _make_api()
    return _thread_local.api


# ============================================================
# 基础工具
# ============================================================

def ensure_dirs() -> None:
    for path in [PARQUET_DIR, META_DIR, LOG_DIR]:
        path.mkdir(parents=True, exist_ok=True)


def today_date_str() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def normalize_code(code: str) -> str:
    code = str(code).strip()
    if "." in code:
        code = code.split(".")[-1]
    return code.zfill(6)


def code_to_market(code: str) -> int:
    """0=深市  1=沪市"""
    c = normalize_code(code)
    if c[:3] in ("600", "601", "603", "605", "688", "689"):
        return 1
    return 0


def code_to_exchange(code: str) -> str:
    return "sh" if code_to_market(code) == 1 else "sz"


def next_day_str(date_str: str) -> str:
    return (pd.to_datetime(date_str) + timedelta(days=1)).strftime("%Y-%m-%d")


# ============================================================
# 日志与状态（带锁，线程安全）
# ============================================================

def append_failed(code: str, name: str, reason: str) -> None:
    row = pd.DataFrame([{
        "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "code": normalize_code(code),
        "name": str(name),
        "reason": str(reason)[:1000],
    }])
    with _write_lock:
        if FAILED_FILE.exists():
            old = pd.read_csv(FAILED_FILE, dtype={"code": str})
            out = pd.concat([old, row], ignore_index=True)
        else:
            out = row
        out.to_csv(FAILED_FILE, index=False, encoding="utf-8-sig")


def append_request_log(code, name, start_date, end_date, status, rows=0, reason=""):
    row = pd.DataFrame([{
        "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "code": normalize_code(code),
        "name": str(name),
        "start_date": str(start_date),
        "end_date": str(end_date),
        "status": str(status),
        "rows": int(rows),
        "reason": str(reason)[:500],
    }])
    with _write_lock:
        file_exists = REQUEST_LOG_FILE.exists()
        row.to_csv(
            REQUEST_LOG_FILE,
            mode="a",
            header=not file_exists,
            index=False,
            encoding="utf-8-sig" if not file_exists else "utf-8",
        )


def load_request_state() -> dict[str, str]:
    if not REQUEST_STATE_FILE.exists():
        return {}
    df = pd.read_csv(REQUEST_STATE_FILE, dtype=str)
    if df.empty or "code" not in df.columns or "requested_until" not in df.columns:
        return {}
    df["code"] = df["code"].apply(normalize_code)
    df["requested_until"] = df["requested_until"].astype(str)
    df = df.dropna(subset=["code", "requested_until"])
    df = df[df["requested_until"].str.match(r"^\d{4}-\d{2}-\d{2}$", na=False)]
    df = df.sort_values(["code", "requested_until"]).drop_duplicates(subset=["code"], keep="last")
    return dict(zip(df["code"], df["requested_until"]))


def save_request_state(
    request_state: dict[str, str],
    stock_info: dict[str, dict[str, str]],
) -> None:
    # 全程加锁：读-合并-写一气呵成，防止多线程竞态
    with _state_lock:
        existing: dict[str, str] = {}
        if REQUEST_STATE_FILE.exists():
            try:
                ex_df = pd.read_csv(REQUEST_STATE_FILE, dtype=str)
                if not ex_df.empty and "code" in ex_df.columns:
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
                "exchange": code_to_exchange(code),
                "name": info.get("name", ""),
                "requested_until": requested_until,
                "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            })
        out = pd.DataFrame(rows, columns=["code", "exchange", "name", "requested_until", "updated_at"])
        out.to_csv(REQUEST_STATE_FILE, index=False, encoding="utf-8-sig")


def advance_request_state(
    request_state: dict[str, str],
    stock_info: dict[str, dict[str, str]],
    code: str,
    name: str,
    requested_until: str,
) -> None:
    code = normalize_code(code)
    with _state_lock:
        old = request_state.get(code)
        if old is None or requested_until > old:
            request_state[code] = requested_until
        stock_info[code] = {"name": name}
        # 在锁内直接写盘（_state_lock 已持有，save_request_state 内不再重复加锁）
        _save_request_state_locked(request_state, stock_info)


def _save_request_state_locked(
    request_state: dict[str, str],
    stock_info: dict[str, dict[str, str]],
) -> None:
    """调用者必须已持有 _state_lock。"""
    existing: dict[str, str] = {}
    if REQUEST_STATE_FILE.exists():
        try:
            ex_df = pd.read_csv(REQUEST_STATE_FILE, dtype=str)
            if not ex_df.empty and "code" in ex_df.columns:
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
            "exchange": code_to_exchange(code),
            "name": info.get("name", ""),
            "requested_until": requested_until,
            "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        })
    out = pd.DataFrame(rows, columns=["code", "exchange", "name", "requested_until", "updated_at"])
    out.to_csv(REQUEST_STATE_FILE, index=False, encoding="utf-8-sig")


# ============================================================
# 本地 Parquet 存储
# ============================================================

def code_to_parquet_path(code: str) -> Path:
    return PARQUET_DIR / f"{normalize_code(code)}.parquet"


def read_stock_data(code: str) -> pd.DataFrame:
    path = code_to_parquet_path(code)
    if not path.exists():
        return pd.DataFrame()
    df = pd.read_parquet(path)
    if df.empty:
        return df
    df["date"] = pd.to_datetime(df["date"])
    df["code"] = df["code"].astype(str).str.zfill(6)
    return df


def write_stock_data(code: str, df: pd.DataFrame) -> None:
    if df.empty:
        return
    path = code_to_parquet_path(code)
    out = df.copy()
    out["date"] = pd.to_datetime(out["date"])
    out["code"] = out["code"].astype(str).str.zfill(6)
    out = out.drop_duplicates(subset=["code", "date"], keep="last")
    out = out.sort_values(["code", "date"]).reset_index(drop=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    with _write_lock:
        out.to_parquet(path, index=False)


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
# 股票列表
# ============================================================

def get_stock_list() -> pd.DataFrame:
    """
    优先复用已有的 stock_list.csv（由 baostock 生成，含 ipoDate/status 过滤）。
    若不存在则从 TDX 按代码前缀抓取。
    """
    if STOCK_LIST_FILE.exists():
        df = pd.read_csv(STOCK_LIST_FILE, dtype=str)
        if not df.empty and "code" in df.columns:
            df["code"] = df["code"].apply(normalize_code)
            if "name" not in df.columns and "code_name" in df.columns:
                df["name"] = df["code_name"]
            df = df[df["code"].str.match(r"^\d{6}$", na=False)]
            print(f"复用已有股票列表，共 {len(df)} 只")
            return df[["code", "name"]].drop_duplicates("code").sort_values("code").reset_index(drop=True)

    print("stock_list.csv 不存在，从 TDX 获取股票列表...")
    api = get_api()

    SZ_PREFIXES = ("000", "001", "002", "003", "300", "301", "302")
    SH_PREFIXES = ("600", "601", "603", "605", "688", "689")

    rows = []
    for market, prefixes in ((0, SZ_PREFIXES), (1, SH_PREFIXES)):
        start = 0
        while True:
            batch = api.get_security_list(market, start)
            if not batch:
                break
            for s in batch:
                code = str(s.get("code", "")).zfill(6)
                if code[:3] in prefixes:
                    rows.append({"code": code, "name": s.get("name", "")})
            start += len(batch)

    df = pd.DataFrame(rows).drop_duplicates("code").sort_values("code").reset_index(drop=True)
    df.to_csv(STOCK_LIST_FILE, index=False, encoding="utf-8-sig")
    print(f"TDX 获取股票列表，共 {len(df)} 只")
    return df


# ============================================================
# 从 parquet 补全缺失的 request_state
# ============================================================

def rebuild_request_state_from_parquet(request_state: dict[str, str]) -> int:
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


# ============================================================
# pytdx 历史日线获取
# ============================================================

def _tdx_bars_to_df(bars: list, code: str) -> pd.DataFrame:
    if not bars:
        return pd.DataFrame()
    df = pd.DataFrame(bars)
    df["date"] = pd.to_datetime(df["datetime"].str[:10])
    df["code"] = normalize_code(code)
    df = df.rename(columns={"vol": "volume"})
    for col in ("open", "high", "low", "close", "volume", "amount"):
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    # pytdx vol 单位是手（1手=100股），统一转为股数与 baostock 保持一致
    if "volume" in df.columns:
        df["volume"] = df["volume"] * 100
    cols = [c for c in ("date", "code", "open", "high", "low", "close", "volume", "amount") if c in df.columns]
    return df[cols].sort_values("date").reset_index(drop=True)


def fetch_recent_bars(code: str, days: int = 30) -> pd.DataFrame:
    """取最近 days 条日线（含今日），用于增量更新。"""
    market = code_to_market(code)
    norm = normalize_code(code)
    for attempt in range(3):
        try:
            api = get_api() if attempt == 0 else reset_api()
            bars = api.get_security_bars(9, market, norm, 0, days)
            return _tdx_bars_to_df(bars, code)
        except Exception:
            pass
    return pd.DataFrame()


def fetch_full_history(code: str) -> pd.DataFrame:
    """分批拉取全部历史日线（用于首次导入）。"""
    market = code_to_market(code)
    norm = normalize_code(code)
    all_bars: list = []
    offset = 0
    for attempt in range(3):
        try:
            api = get_api() if attempt == 0 else reset_api()
            break
        except Exception:
            if attempt == 2:
                return pd.DataFrame()

    while True:
        try:
            api = get_api()
            bars = api.get_security_bars(9, market, norm, offset, BARS_PER_CALL)
        except Exception:
            try:
                api = reset_api()
                bars = api.get_security_bars(9, market, norm, offset, BARS_PER_CALL)
            except Exception:
                break
        if not bars:
            break
        all_bars = bars + all_bars   # 旧数据前置
        if len(bars) < BARS_PER_CALL:
            break
        offset += BARS_PER_CALL

    return _tdx_bars_to_df(all_bars, code)


# ============================================================
# 流通股本缓存（用于计算换手率）
# ============================================================

FINANCE_CACHE_FILE = META_DIR / "finance_cache.csv"
_finance_cache: dict[str, float] = {}   # {code: liutongguben}
_finance_cache_lock = threading.Lock()


def load_finance_cache() -> None:
    """从磁盘加载流通股本缓存到内存。"""
    global _finance_cache
    if not FINANCE_CACHE_FILE.exists():
        return
    try:
        df = pd.read_csv(FINANCE_CACHE_FILE, dtype=str)
        df["code"] = df["code"].apply(normalize_code)
        df["liutongguben"] = pd.to_numeric(df["liutongguben"], errors="coerce")
        df = df.dropna(subset=["liutongguben"])
        with _finance_cache_lock:
            _finance_cache = dict(zip(df["code"], df["liutongguben"]))
    except Exception:
        pass


def save_finance_cache() -> None:
    """将内存中的流通股本缓存落盘。"""
    with _finance_cache_lock:
        if not _finance_cache:
            return
        rows = [{"code": c, "liutongguben": v} for c, v in sorted(_finance_cache.items())]
    out = pd.DataFrame(rows)
    with _write_lock:
        out.to_csv(FINANCE_CACHE_FILE, index=False, encoding="utf-8-sig")


def get_liutongguben(code: str) -> float | None:
    """
    获取股票流通股本（股数）。
    先查内存缓存，缺失时从 pytdx 实时拉取并写入缓存。
    """
    code = normalize_code(code)
    with _finance_cache_lock:
        if code in _finance_cache:
            return _finance_cache[code]

    # 缓存未命中，从 pytdx 拉取
    try:
        market = code_to_market(code)
        api = get_api()
        info = api.get_finance_info(market, code)
        val = info.get("liutongguben") if info else None
        if val and val > 0:
            with _finance_cache_lock:
                _finance_cache[code] = float(val)
            return float(val)
    except Exception:
        pass
    return None


# ============================================================
# 计算 pct_chg（涨跌幅）
# ============================================================

def compute_pct_chg(df: pd.DataFrame) -> pd.DataFrame:
    """从 close 列计算 pct_chg，存入同名列。"""
    if df.empty or "close" not in df.columns:
        return df
    df = df.sort_values("date").copy()
    df["pct_chg"] = df["close"].pct_change() * 100
    return df


# ============================================================
# 更新单只股票（供线程池调用）
# ============================================================

def update_one_stock(
    code: str,
    name: str,
    end_date: str,
    request_state: dict[str, str],
    stock_info: dict[str, dict[str, str]],
) -> tuple[str, int]:
    code = normalize_code(code)
    requested_until = request_state.get(code)

    # 快速跳过：已更新到 end_date，且 parquet 文件确实存在
    if requested_until and requested_until >= end_date:
        if code_to_parquet_path(code).exists():
            return "skip", 0

    old_df = read_stock_data(code)

    # 计算实际需要从哪天开始
    if not old_df.empty:
        last_date = pd.to_datetime(old_df["date"]).max()
        start_date = (last_date + timedelta(days=1)).strftime("%Y-%m-%d")
        if requested_until:
            start_date = max(start_date, next_day_str(requested_until))
    else:
        start_date = START_DATE

    if start_date > end_date:
        return "skip", 0

    append_request_log(code, name, start_date, end_date, "started")

    # 获取数据
    if old_df.empty:
        # 首次：拉全部历史
        new_df = fetch_full_history(code)
    else:
        # 增量：拉最近 N 天（多留余量覆盖节假日）
        gap_days = (pd.to_datetime(end_date) - pd.to_datetime(start_date)).days + 30
        fetch_count = min(max(gap_days, 30), BARS_PER_CALL)
        new_df = fetch_recent_bars(code, days=fetch_count)
        # 只保留新日期
        if not new_df.empty and not old_df.empty:
            cutoff = pd.to_datetime(old_df["date"]).max()
            new_df = new_df[pd.to_datetime(new_df["date"]) > cutoff]

    if new_df.empty:
        append_request_log(code, name, start_date, end_date, "empty")
        return "empty", 0

    # 计算换手率：volume（股）/ 流通股本 * 100
    liutongguben = get_liutongguben(code)
    if liutongguben and liutongguben > 0 and "volume" in new_df.columns:
        new_df = new_df.copy()
        new_df["turn"] = new_df["volume"] / liutongguben * 100

    merged = merge_stock_data(old_df, new_df)
    merged = compute_pct_chg(merged)
    write_stock_data(code, merged)
    # 只在成功写入 parquet 后才更新状态，防止中途停止导致数据缺失但状态已完成
    advance_request_state(request_state, stock_info, code, name, end_date)
    append_request_log(code, name, start_date, end_date, "updated", rows=len(new_df))
    return "updated", len(new_df)


# ============================================================
# 修复缺失的换手率（turn）
# ============================================================

def repair_missing_turn() -> int:
    """扫描所有 parquet，对 turn 为 NaN 的行补算换手率，返回修复的文件数。"""
    need_repair = []
    for f in sorted(PARQUET_DIR.glob("*.parquet")):
        try:
            df = pd.read_parquet(f)
            if "turn" not in df.columns or df["turn"].isna().any():
                need_repair.append(f.stem)
        except Exception:
            pass

    if not need_repair:
        return 0

    print(f"需要补算 turn 的股票: {len(need_repair)} 只，开始修复...")

    def _repair_one(code: str) -> bool:
        try:
            df = pd.read_parquet(code_to_parquet_path(code))
            if df.empty:
                return False
            if "turn" not in df.columns:
                df = df.copy()
                df["turn"] = float("nan")
            mask = df["turn"].isna() & df["volume"].notna()
            if not mask.any():
                return False
            lb = get_liutongguben(code)
            if not lb or lb <= 0:
                return False
            df = df.copy()
            df.loc[mask, "turn"] = df.loc[mask, "volume"] / lb * 100
            write_stock_data(code, df)
            return True
        except Exception:
            return False

    fixed = 0
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        futures = {pool.submit(_repair_one, code): code for code in need_repair}
        for future in tqdm(as_completed(futures), total=len(need_repair), desc="修复 turn"):
            if future.result():
                fixed += 1

    save_finance_cache()
    print(f"turn 修复完成: {fixed}/{len(need_repair)} 只")
    return fixed


# ============================================================
# 主入口
# ============================================================

def main() -> None:
    ensure_dirs()
    end_date = today_date_str()

    print("开始更新 A 股日线数据 - pytdx（通达信协议）")
    print(f"历史兜底起始日期: {START_DATE}（每支股票按需增量更新）")
    print(f"结束日期: {end_date}")
    print(f"主存储: {PARQUET_DIR.resolve()}")
    print(f"并发线程: {WORKERS}")

    stock_list = get_stock_list()
    print(f"股票数量: {len(stock_list)}")

    request_state = load_request_state()
    filled = rebuild_request_state_from_parquet(request_state)
    if filled > 0:
        print(f"从 parquet 补充了 {filled} 只股票的进度记录")

    stock_info: dict[str, dict[str, str]] = {
        normalize_code(row["code"]): {"name": str(row["name"])}
        for _, row in stock_list.iterrows()
    }

    updated_count = 0
    updated_rows = 0
    skip_count = 0
    empty_count = 0
    failed_count = 0

    load_finance_cache()
    print(f"流通股本缓存: {len(_finance_cache)} 只")
    repair_missing_turn()

    tasks = [
        (normalize_code(row["code"]), str(row["name"]))
        for _, row in stock_list.iterrows()
    ]

    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        futures = {
            pool.submit(update_one_stock, code, name, end_date, request_state, stock_info): (code, name)
            for code, name in tasks
        }

        for future in tqdm(as_completed(futures), total=len(futures), desc="更新行情"):
            code, name = futures[future]
            try:
                status, rows = future.result()
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

    # 最终落盘
    save_request_state(request_state, stock_info)
    save_finance_cache()

    print()
    print("更新完成")
    print(f"更新股票数: {updated_count}")
    print(f"新增记录数: {updated_rows}")
    print(f"跳过股票数: {skip_count}")
    print(f"空数据股票数: {empty_count}")
    print(f"失败股票数: {failed_count}")


if __name__ == "__main__":
    main()
