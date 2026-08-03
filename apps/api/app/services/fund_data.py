"""基金每日净值同步与资产快照服务。

历史净值来源优先级：
1. 东方财富天天基金 f10/lsjz 分页接口（按 20 条/页直到 cutoff 或 TotalCount）；
2. AKShare ``fund_open_fund_info_em(period='5年')`` 回退（同一东财数据，单次取 5 年）；
3. 968xxx 香港基金尝试 AKShare ``fund_hk_fund_hist_em``。

约束：
- 单页请求失败重试 3 次，请求间隔限速 0.25s；
- 只 upsert 缺失日期，已有最新数据不会被 AKShare 回退数据覆盖；
- 每只基金的覆盖范围与断点记录在 ``nav_sync_status`` 表。
"""

from __future__ import annotations

import json
import logging
import re
import time
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.error import URLError
from urllib.request import Request, urlopen

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.models import FundNav, Instrument, NavSyncStatus, PortfolioSnapshot, Position

logger = logging.getLogger(__name__)
FUND_NAV_URL = "https://api.fund.eastmoney.com/f10/lsjz?fundCode={code}&pageIndex=1&pageSize=1"
FUND_NAV_HISTORY_URL = (
    "https://api.fund.eastmoney.com/f10/lsjz?fundCode={code}&pageIndex={page}&pageSize={page_size}"
)
FUND_NAV_FAST_URL = "https://fund.eastmoney.com/pingzhongdata/{code}.js"
_FAST_TREND_PATTERN = re.compile(r"var\s+Data_netWorthTrend\s*=\s*(.*?);", re.S)
_FAST_ACC_PATTERN = re.compile(r"var\s+Data_ACWorthTrend\s*=\s*(.*?);", re.S)

# 历史回填参数
PAGE_SIZE = 20
MAX_YEARS = 5
DEFAULT_YEARS = 5
MAX_DAYS = 366 * MAX_YEARS
REQUEST_INTERVAL = 0.25
MAX_RETRIES = 3
RETRY_BACKOFF = 0.5
# 5 年约 61 个月，按每月 ~21 个交易日估算的分页上限，防止 TotalCount 异常时死循环
MAX_PAGES = 100


def _to_decimal(value: str | None) -> Decimal | None:
    if not value:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _request_json(url: str, code: str, timeout: int = 15) -> dict | None:
    request = Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 money-personal-dashboard/0.1",
            "Referer": f"https://fundf10.eastmoney.com/jjjz_{code}.html",
        },
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            text = response.read().decode("utf-8", errors="ignore")
        import json

        return json.loads(text)
    except (OSError, URLError, TimeoutError, ValueError) as exc:
        logger.warning("基金 %s 数据请求失败：%s", code, exc)
        return None


def _request_json_with_retry(url: str, code: str, timeout: int = 15) -> dict | None:
    """带重试与限速的分页请求：最多重试 MAX_RETRIES 次，请求间隔 REQUEST_INTERVAL 秒。"""
    for attempt in range(MAX_RETRIES):
        if attempt > 0:
            time.sleep(RETRY_BACKOFF * attempt)
        data = _request_json(url, code, timeout)
        if data is not None:
            time.sleep(REQUEST_INTERVAL)
            return data
    return None


def _request_text_with_retry(url: str, code: str, timeout: int = 15) -> str | None:
    """请求文本资源并重试，用于东方财富单文件历史净值。"""
    request = Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 money-personal-dashboard/0.1",
            "Referer": "https://fund.eastmoney.com/",
        },
    )
    for attempt in range(MAX_RETRIES):
        if attempt > 0:
            time.sleep(RETRY_BACKOFF * attempt)
        try:
            with urlopen(request, timeout=timeout) as response:
                text = response.read().decode("utf-8", errors="ignore")
            time.sleep(REQUEST_INTERVAL)
            return text
        except (OSError, URLError, TimeoutError) as exc:
            logger.warning("基金 %s 快速历史请求失败（第 %d 次）：%s", code, attempt + 1, exc)
    return None


def fetch_nav_history_fast(
    code: str,
    *,
    days: int | None = None,
    years: int | None = None,
    end_date: date | None = None,
    timeout: int = 15,
) -> tuple[list[dict], str | None]:
    """从 pingzhongdata 单文件一次取得历史净值。

    该接口通常一次返回成立以来数据，适合批量历史回填；找不到普通基金数据时
    返回明确错误，由调用方回退 lsjz 分页或港基接口。累计净值按时间戳合并。
    """
    text = _request_text_with_retry(FUND_NAV_FAST_URL.format(code=code), code, timeout)
    if not text:
        return [], f"快速历史接口请求失败（重试 {MAX_RETRIES} 次）"
    trend_match = _FAST_TREND_PATTERN.search(text)
    if trend_match is None:
        return [], "快速历史接口未返回 Data_netWorthTrend"
    try:
        trend = json.loads(trend_match.group(1))
        accumulated_match = _FAST_ACC_PATTERN.search(text)
        accumulated = json.loads(accumulated_match.group(1)) if accumulated_match else []
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        return [], f"快速历史接口数据解析失败：{exc}"

    accumulated_by_timestamp: dict[int, Decimal] = {}
    for item in accumulated if isinstance(accumulated, list) else []:
        if not isinstance(item, list) or len(item) < 2:
            continue
        value = _to_decimal(str(item[1]))
        if value is not None:
            accumulated_by_timestamp[int(item[0])] = value

    _, cutoff = resolve_window(days=days, years=years)
    rows: list[dict] = []
    for item in trend if isinstance(trend, list) else []:
        if not isinstance(item, dict):
            continue
        try:
            timestamp = int(item.get("x"))
            nav_date = datetime.fromtimestamp(timestamp / 1000, tz=timezone.utc).date()
        except (TypeError, ValueError, OSError):
            continue
        if nav_date < cutoff or (end_date is not None and nav_date >= end_date):
            continue
        unit_nav = _to_decimal(str(item.get("y")))
        if unit_nav is None:
            continue
        rows.append(
            {
                "code": code,
                "nav_date": nav_date,
                "unit_nav": unit_nav,
                "accumulated_nav": accumulated_by_timestamp.get(timestamp),
                "daily_growth_rate": _to_decimal(str(item.get("equityReturn"))),
                "source": "eastmoney_fast",
            }
        )
    rows.sort(key=lambda item: item["nav_date"])
    if not rows:
        return [], "快速历史接口没有目标区间内的净值"
    return rows, None


def _parse_nav_row(code: str, row: dict) -> dict | None:
    unit_nav = _to_decimal(row.get("DWJZ"))
    if unit_nav is None:
        return None
    try:
        nav_date = datetime.strptime(str(row.get("FSRQ"))[:10], "%Y-%m-%d").date()
    except ValueError:
        return None
    return {
        "code": code,
        "nav_date": nav_date,
        "unit_nav": unit_nav,
        "accumulated_nav": _to_decimal(row.get("LJJZ")),
        "daily_growth_rate": _to_decimal(row.get("JZZZL")),
        "source": "eastmoney",
    }


def resolve_window(
    days: int | None = None,
    years: int | None = None,
    today: date | None = None,
) -> tuple[int, date]:
    """把 days/years 统一成 (天数, cutoff 日期)，上限 5 年。"""
    if years is not None:
        days = min(int(years * 365.25), MAX_DAYS)
    elif days is None:
        days = 365
    else:
        days = min(int(days), MAX_DAYS)
    days = max(days, 1)
    cutoff = (today or date.today()) - timedelta(days=days)
    return days, cutoff


def fetch_latest_nav(code: str, timeout: int = 10) -> dict | None:
    """从天天基金公开接口读取最新单位净值。"""
    data = _request_json(FUND_NAV_URL.format(code=code), code, timeout)
    if data is None:
        return None
    rows = (data.get("Data") or {}).get("LSJZList") or []
    if not rows:
        return None
    return _parse_nav_row(code, rows[0])


def fetch_nav_history(
    code: str,
    days: int | None = None,
    years: int | None = None,
    end_date: date | None = None,
    timeout: int = 15,
) -> tuple[list[dict], str | None]:
    """按页读取基金历史净值，直到覆盖 cutoff 或达到 TotalCount。

    - days / years 任选其一，上限 5 年；
    - end_date 用于断点续传：只取该日期（不含）之前的数据；
    - 返回 (按日期升序的行列表, 错误信息)。错误信息为 None 表示成功。
    """
    _, cutoff = resolve_window(days=days, years=years)
    rows: list[dict] = []
    seen: set[date] = set()
    total_count: int | None = None
    fetched = 0
    last_error: str | None = None

    for page in range(1, MAX_PAGES + 1):
        data = _request_json_with_retry(
            FUND_NAV_HISTORY_URL.format(code=code, page=page, page_size=PAGE_SIZE),
            code,
            timeout,
        )
        if data is None:
            last_error = f"第 {page} 页请求失败（重试 {MAX_RETRIES} 次）"
            break
        payload = data.get("Data") or {}
        if total_count is None:
            # TotalCount 可能在外层或 Data 内，也可能缺失（缺失时按 cutoff/空页终止）
            raw_total = data.get("TotalCount") or payload.get("TotalCount")
            try:
                total_count = int(raw_total) if raw_total else None
            except (TypeError, ValueError):
                total_count = None
        items = payload.get("LSJZList") or []
        if not items:
            break
        fetched += len(items)
        reached_cutoff = False
        for item in items:
            parsed = _parse_nav_row(code, item)
            if parsed is None:
                continue
            nav_date = parsed["nav_date"]
            if end_date is not None and nav_date >= end_date:
                continue
            if nav_date < cutoff:
                reached_cutoff = True
                continue
            if nav_date not in seen:
                seen.add(nav_date)
                rows.append(parsed)
        # 翻页终止条件：已覆盖 cutoff、本页不满、或已取完全部 TotalCount
        if reached_cutoff or len(items) < PAGE_SIZE or (total_count is not None and fetched >= total_count):
            break
    rows.sort(key=lambda item: item["nav_date"])
    return rows, last_error


def _fetch_nav_history_akshare(code: str, years: int = DEFAULT_YEARS) -> tuple[list[dict], str | None]:
    """AKShare 回退：fund_open_fund_info_em(period='5年')，单次取回 5 年单位净值。"""
    try:
        import akshare as ak
    except ImportError:
        return [], "akshare 未安装"
    period = f"{min(years, MAX_YEARS)}年"
    try:
        df = ak.fund_open_fund_info_em(symbol=code, indicator="单位净值走势", period=period)
    except Exception as exc:  # AKShare 内部异常类型不固定，统一兜底
        return [], f"akshare fund_open_fund_info_em 失败：{exc}"
    time.sleep(REQUEST_INTERVAL)
    if df is None or df.empty:
        return [], "akshare fund_open_fund_info_em 返回空数据"
    rows: list[dict] = []
    for record in df.to_dict("records"):
        unit_nav = _to_decimal(record.get("单位净值"))
        if unit_nav is None:
            continue
        try:
            nav_date = datetime.strptime(str(record.get("净值日期"))[:10], "%Y-%m-%d").date()
        except ValueError:
            continue
        rows.append(
            {
                "code": code,
                "nav_date": nav_date,
                "unit_nav": unit_nav,
                "accumulated_nav": None,
                "daily_growth_rate": _to_decimal(record.get("日增长率")),
                "source": "akshare",
            }
        )
    rows.sort(key=lambda item: item["nav_date"])
    return rows, None


def _fetch_nav_history_akshare_hk(code: str) -> tuple[list[dict], str | None]:
    """968xxx 香港基金回退：fund_hk_fund_hist_em 历史净值明细。"""
    try:
        import akshare as ak
    except ImportError:
        return [], "akshare 未安装"
    try:
        df = ak.fund_hk_fund_hist_em(code=code, symbol="历史净值明细")
    except Exception as exc:
        return [], f"akshare fund_hk_fund_hist_em 失败：{exc}"
    time.sleep(REQUEST_INTERVAL)
    if df is None or df.empty:
        return [], "akshare fund_hk_fund_hist_em 返回空数据"
    rows: list[dict] = []
    for record in df.to_dict("records"):
        unit_nav = _to_decimal(record.get("单位净值"))
        if unit_nav is None:
            continue
        try:
            nav_date = datetime.strptime(str(record.get("净值日期"))[:10], "%Y-%m-%d").date()
        except ValueError:
            continue
        rows.append(
            {
                "code": code,
                "nav_date": nav_date,
                "unit_nav": unit_nav,
                "accumulated_nav": None,
                "daily_growth_rate": _to_decimal(record.get("日增长率")),
                "source": "akshare_hk",
            }
        )
    rows.sort(key=lambda item: item["nav_date"])
    return rows, None


def fetch_nav_history_with_fallback(
    code: str,
    days: int | None = None,
    years: int | None = None,
    end_date: date | None = None,
    use_fallback: bool = True,
    timeout: int = 15,
) -> tuple[list[dict], str | None, str | None]:
    """快速历史 → lsjz 分页 → AKShare/港基回退。

    返回 (rows, error, source)。快速源和 lsjz 都属于东方财富主源，均可修正
    历史值；AKShare 只补缺失日期，不覆盖主源数据。
    """
    fast_rows, fast_error = fetch_nav_history_fast(
        code, days=days, years=years, end_date=end_date, timeout=timeout
    )
    if fast_rows:
        return fast_rows, None, "eastmoney_fast"

    rows, error = fetch_nav_history(code, days=days, years=years, end_date=end_date, timeout=timeout)
    if rows or not use_fallback:
        return rows, error or fast_error, "eastmoney" if rows else None

    _, cutoff = resolve_window(days=days, years=years)
    years_value = years if years is not None else MAX_YEARS
    fb_rows, fb_error = _fetch_nav_history_akshare(code, years=years_value)
    source = "akshare"
    if not fb_rows and code.startswith("968"):
        fb_rows, fb_error = _fetch_nav_history_akshare_hk(code)
        source = "akshare_hk"
    if end_date is not None:
        fb_rows = [row for row in fb_rows if row["nav_date"] < end_date]
    fb_rows = [row for row in fb_rows if row["nav_date"] >= cutoff]
    if fb_rows:
        logger.info(
            "基金 %s 主历史接口失败（快速=%s；分页=%s），AKShare 回退成功：%d 行",
            code,
            fast_error,
            error,
            len(fb_rows),
        )
        return fb_rows, None, source
    errors = [message for message in (fast_error, error, fb_error) if message]
    return [], "；".join(errors) or "所有历史净值源均未返回数据", None


def upsert_nav_rows(db: Session, instrument: Instrument, rows: list[dict]) -> tuple[int, int]:
    """只 upsert 缺失日期：已有记录仅在来源不是 AKShare 回退时才刷新。

    返回 (新增行数, 更新行数)。
    """
    if not rows:
        return 0, 0
    nav_dates = [row["nav_date"] for row in rows]
    existing = db.scalars(
        select(FundNav).where(
            FundNav.instrument_id == instrument.id,
            FundNav.nav_date.in_(nav_dates),
        )
    ).all()
    existing_map = {nav.nav_date: nav for nav in existing}
    inserted = 0
    updated = 0
    for row in rows:
        nav = existing_map.get(row["nav_date"])
        if nav is None:
            db.add(
                FundNav(
                    instrument_id=instrument.id,
                    nav_date=row["nav_date"],
                    unit_nav=row["unit_nav"],
                    accumulated_nav=row["accumulated_nav"],
                    daily_growth_rate=row["daily_growth_rate"],
                    source=row["source"],
                )
            )
            inserted += 1
            continue
        # 已有最新数据不被 AKShare 回退覆盖，仅东财主源可修正历史值
        if row["source"] in ("akshare", "akshare_hk"):
            continue
        nav.unit_nav = row["unit_nav"]
        nav.accumulated_nav = row["accumulated_nav"]
        nav.daily_growth_rate = row["daily_growth_rate"]
        nav.source = row["source"]
        updated += 1
    return inserted, updated


def get_nav_sync_status(db: Session, instrument_id: int) -> NavSyncStatus | None:
    return db.scalar(select(NavSyncStatus).where(NavSyncStatus.instrument_id == instrument_id))


def refresh_nav_sync_status(
    db: Session,
    instrument: Instrument,
    *,
    target_start: date,
    status: str,
    next_end_date: date | None = None,
    source: str | None = None,
    error: str | None = None,
) -> NavSyncStatus:
    """根据库内实际净值刷新覆盖状态（earliest/latest/row_count 以库为准）。"""
    db.flush()  # session 关闭 autoflush，聚合统计前需要先把待写入的净值刷入
    earliest, latest, row_count = db.execute(
        select(func.min(FundNav.nav_date), func.max(FundNav.nav_date), func.count(FundNav.id)).where(
            FundNav.instrument_id == instrument.id
        )
    ).one()
    record = get_nav_sync_status(db, instrument.id)
    if record is None:
        record = NavSyncStatus(instrument_id=instrument.id)
        db.add(record)
    record.status = status
    record.target_start_date = target_start
    record.earliest_nav_date = earliest
    record.latest_nav_date = latest
    record.row_count = row_count or 0
    record.next_end_date = next_end_date
    record.last_source = source or record.last_source
    record.last_error = error
    record.last_synced_at = datetime.now()
    return record


def backfill_fund_nav_history(
    db: Session,
    instrument: Instrument,
    years: int = DEFAULT_YEARS,
    resume: bool = True,
    use_fallback: bool = True,
) -> dict[str, Any]:
    """单只基金的断点回填：默认 5 年，缺哪段补哪段。

    - resume=True 且状态为 partial 时，从 next_end_date 继续向前回填；
    - 已 complete 且 earliest <= target_start 时跳过；
    - 全部失败时把错误记录到 nav_sync_status。
    """
    years = min(max(years, 1), MAX_YEARS)
    _, target_start = resolve_window(years=years)
    status_record = get_nav_sync_status(db, instrument.id)

    end_date: date | None = None
    if resume and status_record is not None:
        if status_record.status == "complete" and status_record.earliest_nav_date is not None:
            if status_record.earliest_nav_date <= target_start:
                return {
                    "code": instrument.code,
                    "status": "skipped",
                    "inserted": 0,
                    "updated": 0,
                    "rows": 0,
                    "error": None,
                }
        elif status_record.status == "partial" and status_record.next_end_date is not None:
            end_date = status_record.next_end_date

    rows, error, source = fetch_nav_history_with_fallback(
        instrument.code,
        years=years,
        end_date=end_date,
        use_fallback=use_fallback,
    )
    if not rows:
        # 接口正常但没有更早数据（已到该基金净值起点）：视为完成而不是失败
        if error is None and end_date is not None:
            refresh_nav_sync_status(
                db,
                instrument,
                target_start=target_start,
                status="complete",
                next_end_date=None,
                source=source,
                error=None,
            )
            db.commit()
            return {
                "code": instrument.code,
                "status": "complete",
                "inserted": 0,
                "updated": 0,
                "rows": 0,
                "error": None,
            }
        # 已有覆盖已达到目标起点时，本轮没有更早数据属于完成而不是失败。
        # 这常见于上次已写满 5 年，但状态曾因旧快速源错误被标为 failed。
        existing_earliest = status_record.earliest_nav_date if status_record else None
        if existing_earliest is not None and existing_earliest <= target_start:
            refresh_nav_sync_status(
                db,
                instrument,
                target_start=target_start,
                status="complete",
                next_end_date=None,
                source=source,
                error=None,
            )
            db.commit()
            return {
                "code": instrument.code,
                "status": "complete",
                "inserted": 0,
                "updated": 0,
                "rows": 0,
                "error": None,
            }
        refresh_nav_sync_status(
            db,
            instrument,
            target_start=target_start,
            status="failed",
            next_end_date=end_date,
            source=source,
            error=error or "未获取到净值数据",
        )
        db.commit()
        return {
            "code": instrument.code,
            "status": "failed",
            "inserted": 0,
            "updated": 0,
            "rows": 0,
            "error": error,
        }

    inserted, updated = upsert_nav_rows(db, instrument, rows)
    earliest_in_rows = rows[0]["nav_date"]
    # 完成判定：推进到目标起点；或本轮没有任何新增且 earliest 未再前移（已到该基金净值尽头）
    previous_earliest = status_record.earliest_nav_date if status_record else None
    reached_start = earliest_in_rows <= target_start
    no_progress = inserted == 0 and previous_earliest is not None and earliest_in_rows >= previous_earliest
    complete = reached_start or no_progress
    next_end = None if complete else earliest_in_rows
    refresh_nav_sync_status(
        db,
        instrument,
        target_start=target_start,
        status="complete" if complete else "partial",
        next_end_date=next_end,
        source=source,
        error=None,
    )
    db.commit()
    return {
        "code": instrument.code,
        "status": "complete" if complete else "partial",
        "inserted": inserted,
        "updated": updated,
        "rows": len(rows),
        "error": None,
    }


def sync_fund_nav_history(
    db: Session,
    days: int | None = None,
    years: int | None = None,
    resume: bool = True,
    use_fallback: bool = True,
    codes: list[str] | None = None,
) -> dict[str, Any]:
    """同步全部（或指定）基金历史净值，默认近 5 年，断点续传。"""
    if days is None and years is None:
        years = DEFAULT_YEARS
    stmt = select(Instrument).order_by(Instrument.code)
    if codes:
        stmt = stmt.where(Instrument.code.in_(codes))
    instruments = db.scalars(stmt).all()
    resolved_years = years if years is not None else MAX_YEARS
    if days is not None:
        # days 换算成不超过 5 年的等效年限，供 backfill 计算 cutoff
        resolved_years = min((days + 364) // 365, MAX_YEARS)

    results: list[dict[str, Any]] = []
    completed = 0
    partial = 0
    failed = 0
    skipped = 0
    total_inserted = 0
    for instrument in instruments:
        result = backfill_fund_nav_history(
            db,
            instrument,
            years=resolved_years,
            resume=resume,
            use_fallback=use_fallback,
        )
        results.append(result)
        total_inserted += result["inserted"] + result["updated"]
        if result["status"] == "complete":
            completed += 1
        elif result["status"] == "partial":
            partial += 1
        elif result["status"] == "skipped":
            skipped += 1
        else:
            failed += 1
    return {
        "total_funds": len(instruments),
        "completed": completed,
        "partial": partial,
        "skipped": skipped,
        "failed": failed,
        "rows": total_inserted,
        "failures": [r for r in results if r["status"] == "failed"],
        "details": results,
    }


def sync_fund_navs(db: Session) -> dict[str, int | str | None]:
    """同步所有基金最新净值并更新持仓市值。"""
    instruments = db.scalars(select(Instrument).order_by(Instrument.code)).all()
    updated = 0
    failed = 0
    latest_dates: list[date] = []

    for instrument in instruments:
        data = fetch_latest_nav(instrument.code)
        if data is None:
            failed += 1
            continue
        latest_dates.append(data["nav_date"])
        nav = db.scalar(
            select(FundNav).where(
                FundNav.instrument_id == instrument.id,
                FundNav.nav_date == data["nav_date"],
            )
        )
        if nav is None:
            nav = FundNav(instrument_id=instrument.id, nav_date=data["nav_date"], unit_nav=data["unit_nav"])
            db.add(nav)
        nav.unit_nav = data["unit_nav"]
        nav.accumulated_nav = data["accumulated_nav"]
        nav.daily_growth_rate = data["daily_growth_rate"]
        nav.source = data["source"]
        updated += 1

        positions = db.scalars(select(Position).where(Position.instrument_id == instrument.id)).all()
        for position in positions:
            position.latest_nav = data["unit_nav"]
            position.nav_date = data["nav_date"]
            position.market_value = (position.shares * data["unit_nav"]).quantize(Decimal("0.01"))

    # 净值同步不应覆盖根据交易流水计算出的 FIFO 成本，只刷新市值与快照。
    db.commit()
    snapshot_date = _create_snapshot(db)
    return {
        "total_funds": len(instruments),
        "updated": updated,
        "failed": failed,
        "latest_nav_date": max(latest_dates).isoformat() if latest_dates else None,
        "snapshot_date": snapshot_date.isoformat() if snapshot_date else None,
    }


def _create_snapshot(db: Session) -> date | None:
    """根据当前持仓生成组合快照。"""
    total_cost, total_market_value, snapshot_date = db.execute(
        select(func.sum(Position.cost), func.sum(Position.market_value), func.max(Position.nav_date))
    ).one()
    if snapshot_date is None:
        return None
    total_cost = total_cost or Decimal("0")
    total_market_value = total_market_value or total_cost
    total_profit = total_market_value - total_cost

    db.execute(delete(PortfolioSnapshot).where(PortfolioSnapshot.snapshot_date == snapshot_date))
    db.add(
        PortfolioSnapshot(
            snapshot_date=snapshot_date,
            account_id=None,
            total_cost=total_cost,
            total_market_value=total_market_value,
            total_profit=total_profit,
        )
    )
    db.commit()
    return snapshot_date
