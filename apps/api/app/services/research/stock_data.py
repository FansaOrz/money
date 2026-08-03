"""A 股 master 与日线行情同步。

- sync_stock_master：ak.stock_info_a_code_name -> stock_master 表（幂等 upsert）。
- sync_stock_daily：ak.stock_zh_a_daily -> raw/qfq Parquet 数据湖 + stock_daily_bars 断点。
  真正的断点续传：缺省按“从未同步 -> 有同步错误 -> 最久未更新”取下一批，
  优先消费 stock_sync_state.last_code 游标，批次大小由 settings.research_sync_batch_size 控制。
  每只股票独立 try/except，网络失败记录 last_error 后继续下一只。
- get_data_status：各数据域 coverage 汇总，绝不因缺失而伪造数据。
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pandas as pd
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.config import get_settings

from app.models import (
    IndexConstituent,
    IndexMembershipEvent,
    StockDailyBar,
    StockFinancialIndicator,
    StockIndustry,
    StockMaster,
    StockNameHistory,
    StockReportDisclosure,
    StockSyncState,
    StockUniverseSnapshot,
    StockValuation,
)
from app.services.research import ak_fetch, parquet_store

logger = logging.getLogger(__name__)


def _exchange_of(code: str) -> str | None:
    """由 6 位代码推断交易所（粗略规则，仅用于展示过滤）。

    北交所代码段含 43/83/87/88/92（920xxx 为 2024 起启用的新号段）。
    """
    if code.startswith(("60", "68", "9")) and not code.startswith("92"):
        return "sh"
    if code.startswith(("00", "30", "20")):
        return "sz"
    if code.startswith(("4", "8", "92")):
        return "bj"
    return None


def sina_symbol(code: str) -> str:
    """6 位代码 -> 新浪 symbol（sh600000 / sz000001 / bj430047）。"""
    exchange = _exchange_of(code)
    return f"{exchange}{code}" if exchange else f"sh{code}"


def _to_date(value: Any) -> date | None:
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()[:10]
    if not text or text.lower() in {"nat", "nan", "none"}:
        return None
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def _to_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    try:
        return float(str(value).replace(",", "").strip())
    except (ValueError, TypeError):
        return None


def _to_int(value: Any) -> int | None:
    number = _to_float(value)
    if number is None:
        return None
    try:
        return int(number)
    except (OverflowError, ValueError):
        return None


def _cninfo_market(code: str) -> str | None:
    """6 位代码 -> 巨潮预约披露接口的 market 分区。"""
    exchange = _exchange_of(code)
    return {"sh": "沪市", "sz": "深市", "bj": "北交所"}.get(exchange or "")


def _final_status(updated: int, failed: int, processed: int | None = None) -> str:
    """任务终态：有成功也有失败为 partial；全败 failed；无失败 success。

    processed 为实际处理条数（缺省 updated+failed）：为 0 表示本次没有
    处理任何对象，记 partial（通常是恢复位点越界等异常，绝不是成功）。
    """
    if updated > 0 and failed > 0:
        return "partial"
    if failed > 0:
        return "failed"
    if (processed if processed is not None else updated + failed) == 0:
        return "partial"
    return "success"


# ---------------------------------------------------------------------------
# 同步状态记录
# ---------------------------------------------------------------------------

def _begin_task(db: Session, task: str) -> StockSyncState:
    state = db.get(StockSyncState, task)
    if state is None:
        state = StockSyncState(task=task)
        db.add(state)
        db.flush()
    state.status = "running"
    state.started_at = datetime.now(UTC)
    state.finished_at = None
    state.total = 0
    state.updated = 0
    state.failed = 0
    state.detail = None
    db.commit()
    return state


def _finish_task(
    db: Session,
    state: StockSyncState,
    *,
    total: int = 0,
    updated: int = 0,
    failed: int = 0,
    last_code: str | None = None,
    detail: str | None = None,
    status: str = "success",
    clear_last_code: bool = False,
) -> None:
    state.status = status
    state.finished_at = datetime.now(UTC)
    state.total = total
    state.updated = updated
    state.failed = failed
    if last_code is not None:
        state.last_code = last_code
    elif clear_last_code:
        state.last_code = None
    state.detail = detail
    db.commit()


def _progress_task(
    db: Session,
    state: StockSyncState,
    *,
    processed: int,
    total: int,
    updated: int,
    failed: int,
    last_code: str | None = None,
) -> None:
    """批量同步的中途进度落库（状态保持 running）。"""
    state.total = total
    state.updated = updated
    state.failed = failed
    state.detail = json.dumps(
        {"processed": processed, "total": total, "updated": updated, "failed": failed},
        ensure_ascii=False,
    )
    if last_code is not None:
        state.last_code = last_code
    db.commit()


# ---------------------------------------------------------------------------
# master
# ---------------------------------------------------------------------------

def sync_stock_master(db: Session) -> dict[str, Any]:
    """同步 A 股代码/名称主表（全量 upsert）。网络失败时返回 errors，不抛异常。"""
    state = _begin_task(db, "master")
    frame = ak_fetch.fetch_stock_code_name()
    if frame is None:
        _finish_task(db, state, status="failed", detail="数据源不可用（网络或接口异常）")
        return {
            "task": "master",
            "status": "failed",
            "total": 0,
            "updated": 0,
            "errors": ["ak.stock_info_a_code_name 不可用"],
        }

    existing = {row.code: row for row in db.scalars(select(StockMaster)).all()}
    seen: set[str] = set()
    updated = 0
    for _, record in frame.iterrows():
        code = str(record.get("code") or "").strip().zfill(6)
        name = str(record.get("name") or "").strip()
        if len(code) != 6 or not code.isdigit() or not name:
            continue
        seen.add(code)
        row = existing.get(code)
        if row is None:
            db.add(StockMaster(code=code, name=name, exchange=_exchange_of(code)))
            updated += 1
        elif row.name != name:
            row.name = name  # 名称变更以历史表为准，这里只维护“当前名”
            updated += 1
    db.commit()
    _finish_task(db, state, total=len(seen), updated=updated)
    return {"task": "master", "status": "success", "total": len(seen), "updated": updated, "errors": []}


# ---------------------------------------------------------------------------
# 日线
# ---------------------------------------------------------------------------

def parse_sina_daily_frame(code: str, frame: pd.DataFrame) -> pd.DataFrame:
    """把新浪日线 DataFrame 归一化为标准列。

    输出列：code, trade_date, open, high, low, close, volume, amount, outstanding_share, turnover。
    无法解析的行直接丢弃（不填充、不插值）。
    """
    records: list[dict[str, Any]] = []
    for _, row in frame.iterrows():
        trade_date = _to_date(row.get("date"))
        close = _to_float(row.get("close"))
        if trade_date is None or close is None:
            continue
        records.append(
            {
                "code": code,
                "trade_date": trade_date,
                "open": _to_float(row.get("open")),
                "high": _to_float(row.get("high")),
                "low": _to_float(row.get("low")),
                "close": close,
                "volume": _to_int(row.get("volume")),
                "amount": _to_float(row.get("amount")),
                "outstanding_share": _to_float(row.get("outstanding_share")),
                "turnover": _to_float(row.get("turnover")),
            }
        )
    if not records:
        return pd.DataFrame(
            columns=[
                "code", "trade_date", "open", "high", "low", "close",
                "volume", "amount", "outstanding_share", "turnover",
            ]
        )
    return pd.DataFrame.from_records(records)


def _sync_one_daily(
    db: Session,
    code: str,
    *,
    root: Path | None,
    start_date: date | None,
    fetch_raw: bool,
    fetch_qfq: bool,
) -> tuple[bool, str | None]:
    """同步单只股票日线。返回 (是否成功, 错误信息)。"""
    raw_frame: pd.DataFrame | None = None
    if fetch_raw:
        fetched = ak_fetch.fetch_stock_daily_sina(sina_symbol(code), adjust="")
        if fetched is None:
            return False, "raw 行情抓取失败"
        raw_frame = parse_sina_daily_frame(code, fetched)
        if start_date is not None and not raw_frame.empty:
            raw_frame = raw_frame[raw_frame["trade_date"] >= start_date]

    if fetch_qfq:
        fetched_qfq = ak_fetch.fetch_stock_daily_sina(sina_symbol(code), adjust="qfq")
        if fetched_qfq is not None:
            qfq_frame = parse_sina_daily_frame(code, fetched_qfq)
            if start_date is not None and not qfq_frame.empty:
                qfq_frame = qfq_frame[qfq_frame["trade_date"] >= start_date]
            if not qfq_frame.empty:
                # 前复权价会随除权整体变化，整表覆盖
                parquet_store.write_daily(
                    code, qfq_frame, layer=parquet_store.DAILY_QFQ, root=root, incremental=False
                )

    bar = db.get(StockDailyBar, code)
    if bar is None:
        bar = StockDailyBar(code=code)
        db.add(bar)

    if raw_frame is not None and not raw_frame.empty:
        total_rows = parquet_store.write_daily(
            code, raw_frame, layer=parquet_store.DAILY_RAW, root=root, incremental=True
        )
        first, last, rows = parquet_store.daily_coverage(code, root=root)
        bar.first_trade_date = first
        bar.last_trade_date = last
        bar.rows = rows if rows else total_rows
        bar.parquet_path = str(parquet_store.daily_path(code, root=root))
        bar.available_at = datetime.now(UTC)
        bar.last_error = None
    elif fetch_raw:
        # 抓到空数据：可能是新股/停牌，不算失败但也不伪造断点
        bar.last_error = None if raw_frame is not None else "raw 行情抓取失败"

    return True, None


def _select_daily_batch(
    db: Session,
    limit: int,
    *,
    resume_after: str | None = None,
    exclude: set[str] | None = None,
) -> list[str]:
    """选出本批待同步股票（真正的断点续传策略，不再每次取头部 N 只）。

    优先级：从未同步（无断点行）-> 最近同步出错 -> 最久未更新；
    resume_after 为上次中断的代码（严格大于它的才入选），此时只处理游标
    之后且“无断点或有错误”的股票，已干净同步的不重复抓。
    """
    stmt = (
        select(StockMaster.code, StockDailyBar.available_at, StockDailyBar.last_error)
        .outerjoin(StockDailyBar, StockDailyBar.code == StockMaster.code)
    )
    rows = db.execute(stmt).all()
    excluded = exclude or set()

    def group_of(available_at: Any, last_error: Any) -> int:
        # 组序：0 从未同步 / 1 有同步错误 / 2 正常（按最久未更新）
        if available_at is None and last_error is None:
            return 0
        if last_error is not None:
            return 1
        return 2

    def sort_key(row: Any) -> tuple[int, date, str]:
        code, available_at, last_error = row
        stale = date(1970, 1, 1)
        if isinstance(available_at, datetime):
            stale = available_at.date()
        elif isinstance(available_at, date):
            stale = available_at
        return group_of(available_at, last_error), stale, code

    candidates = []
    for row in rows:
        code, available_at, last_error = row
        if code in excluded:
            continue
        group = group_of(available_at, last_error)
        if resume_after is not None and group != 1:
            # 恢复轮：已干净同步（组 2）不重抓；错误股（组 1）无论位置都重试
            if code <= resume_after or group == 2:
                continue
        candidates.append(row)
    candidates.sort(key=sort_key)
    return [row.code for row in candidates[:limit]]


def sync_stock_daily(
    db: Session,
    codes: list[str] | None = None,
    *,
    limit: int | None = None,
    start_date: date | None = None,
    fetch_qfq: bool = True,
    root: Path | None = None,
    resume: bool = True,
) -> dict[str, Any]:
    """批量同步日线（raw 必抓，qfq 可选），断点可恢复。

    - codes 为 None 时进入自动调度：批次大小取 settings.research_sync_batch_size
      （limit 显式传入时优先），按“未同步 -> 有错误 -> 最旧优先”选批；
      若上次任务带错误结束且留有 last_code，resume=True 时从该游标继续；
      一轮无新批可抓时自动开启新一轮（last_code 清零），保证持续滚动覆盖。
    - codes 显式给出时按给定集合同步（不做自动选批）。
    - 单只失败记入 failed 与 StockDailyBar.last_error，不中断整体；
      每只股票处理后进度（processed/total/updated/failed/last_code）落 stock_sync_state。
    """
    state = _begin_task(db, "daily")
    prev = db.get(StockSyncState, "daily")
    if codes is None:
        batch_size = limit if limit is not None and limit > 0 else get_settings().research_sync_batch_size
        resume_after = prev.last_code if (resume and prev.failed > 0) else None
        codes = _select_daily_batch(db, batch_size, resume_after=resume_after)
        if not codes and resume_after is not None:
            # 恢复位点之后已无标的：本轮视为结束，开启新一轮从头滚
            codes = _select_daily_batch(db, batch_size)
            resume_after = None
        wrapped = not codes or resume_after is None
    else:
        codes = sorted(dict.fromkeys(codes))
        if limit is not None and limit > 0:
            codes = codes[:limit]
        wrapped = False

    total_batch = len(codes)
    updated = 0
    failed = 0
    errors: list[str] = []
    last_code: str | None = None
    for idx, code in enumerate(codes, start=1):
        last_code = code
        try:
            ok, error = _sync_one_daily(
                db, code, root=root, start_date=start_date, fetch_raw=True, fetch_qfq=fetch_qfq
            )
        except Exception as exc:  # 防御：单只异常不影响其余
            logger.exception("同步 %s 日线异常", code)
            ok, error = False, str(exc)
        if ok:
            updated += 1
        else:
            failed += 1
            bar = db.get(StockDailyBar, code)
            if bar is None:
                bar = StockDailyBar(code=code)
                db.add(bar)
            bar.last_error = error
            errors.append(f"{code}: {error}")
        db.commit()
        _progress_task(
            db, state, processed=idx, total=total_batch,
            updated=updated, failed=failed, last_code=code,
        )

    status = _final_status(updated, failed, processed=total_batch)
    _finish_task(
        db, state, total=total_batch, updated=updated, failed=failed,
        last_code=None if wrapped else last_code,
        clear_last_code=wrapped,
        detail="; ".join(errors[:20]) or None, status=status,
    )
    return {
        "task": "daily",
        "status": status,
        "total": total_batch,
        "updated": updated,
        "failed": failed,
        "last_code": None if wrapped else last_code,
        "errors": errors,
    }


# ---------------------------------------------------------------------------
# 状态 / coverage
# ---------------------------------------------------------------------------

def _task_state(db: Session, task: str) -> dict[str, Any]:
    state = db.get(StockSyncState, task)
    if state is None:
        return {"status": "never_run"}
    return {
        "status": state.status,
        "started_at": state.started_at,
        "finished_at": state.finished_at,
        "total": state.total,
        "updated": state.updated,
        "failed": state.failed,
        "last_code": state.last_code,
        "detail": state.detail,
    }


def get_data_status(db: Session, root: Path | None = None) -> dict[str, Any]:
    """各数据域 coverage 汇总（真实统计，不估算、不伪造）。"""
    master_count = db.scalar(select(func.count()).select_from(StockMaster)) or 0

    bar_stats = db.execute(
        select(
            func.count(),
            func.min(StockDailyBar.first_trade_date),
            func.max(StockDailyBar.last_trade_date),
            func.count(StockDailyBar.last_error),
        ).select_from(StockDailyBar)
    ).one()
    daily_files = len(parquet_store.list_synced_codes(root=root))

    constituent_rows = db.execute(
        select(IndexConstituent.index_code, func.count())
        .group_by(IndexConstituent.index_code)
    ).all()

    fin_stats = db.execute(
        select(func.count(), func.count(func.distinct(StockFinancialIndicator.code)))
        .select_from(StockFinancialIndicator)
    ).one()
    disclosure_stats = db.execute(
        select(func.count(), func.count(func.distinct(StockReportDisclosure.code)))
        .select_from(StockReportDisclosure)
    ).one()
    valuation_stats = db.execute(
        select(func.count(), func.count(func.distinct(StockValuation.code)))
        .select_from(StockValuation)
    ).one()
    name_stats = db.execute(
        select(func.count(), func.count(func.distinct(StockNameHistory.code)))
        .select_from(StockNameHistory)
    ).one()
    industry_rows = db.execute(
        select(StockIndustry.source, func.count(), func.count(func.distinct(StockIndustry.code)))
        .group_by(StockIndustry.source)
    ).all()
    event_count = db.scalar(select(func.count()).select_from(IndexMembershipEvent)) or 0
    snapshot_count = db.scalar(select(func.count()).select_from(StockUniverseSnapshot)) or 0

    return {
        "generated_at": datetime.now(UTC),
        "master": {
            "stocks": master_count,
            "sync": _task_state(db, "master"),
        },
        "daily": {
            # 断点表覆盖的股票数（含失败记录）
            "stocks_tracked": bar_stats[0],
            "stocks_with_parquet": daily_files,
            "stocks_with_error": bar_stats[3],
            "first_trade_date": bar_stats[1],
            "last_trade_date": bar_stats[2],
            "sync": _task_state(db, "daily"),
        },
        "universe": {
            "constituents": {row[0]: row[1] for row in constituent_rows},
            "membership_events": event_count,
            "snapshots": snapshot_count,
            "sync": _task_state(db, "universe"),
        },
        "fundamentals": {
            "financial_indicator_rows": fin_stats[0],
            "financial_indicator_stocks": fin_stats[1],
            "disclosure_rows": disclosure_stats[0],
            "disclosure_stocks": disclosure_stats[1],
            "valuation_rows": valuation_stats[0],
            "valuation_stocks": valuation_stats[1],
            "name_history_rows": name_stats[0],
            "name_history_stocks": name_stats[1],
            "sync_financial": _task_state(db, "financial"),
            "sync_disclosure": _task_state(db, "disclosure"),
            "sync_valuation": _task_state(db, "valuation"),
            "sync_name_history": _task_state(db, "name_history"),
        },
        "industry": {
            "stocks": sum(row[2] for row in industry_rows),
            "sources": {row[0]: {"rows": row[1], "stocks": row[2]} for row in industry_rows},
            "sync": _task_state(db, "industry"),
        },
    }


def clear_daily(db: Session, code: str, root: Path | None = None) -> None:
    """删除单只股票日线（数据湖文件 + 断点行），仅维护/测试用。"""
    for layer in (parquet_store.DAILY_RAW, parquet_store.DAILY_QFQ):
        path = parquet_store.daily_path(code, layer, root)
        if path.exists():
            path.unlink()
    db.execute(delete(StockDailyBar).where(StockDailyBar.code == code))
    db.commit()


def get_daily_bars(
    db: Session,
    code: str,
    *,
    layer: str = parquet_store.DAILY_RAW,
    start_date: date | None = None,
    end_date: date | None = None,
    root: Path | None = None,
) -> list[dict[str, Any]]:
    """读取单只股票日线（供研究查询；无数据返回空列表）。"""
    frame = parquet_store.read_daily(code, layer, root)
    if frame is None:
        return []
    if start_date is not None:
        frame = frame[frame["trade_date"] >= start_date]
    if end_date is not None:
        frame = frame[frame["trade_date"] <= end_date]
    return frame.to_dict(orient="records")
