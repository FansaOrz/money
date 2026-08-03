"""候选池基金季度重仓/行业披露的可恢复批处理。

与既有 ``sync_fund_holdings``（仅按持仓 Position 选基、无断点）不同，本模块：
- 从候选池 active 成员选基（成员已有对应 Instrument，按 code 关联），
  默认取最新一期候选池；
- 按年度推进：以 ``FundHoldingsSyncStatus``（instrument_id × year）记录
  complete / partial / failed 与错误原因，中断后重跑自动跳过 complete、
  优先重试 failed / partial / 未覆盖的基金；
- 单只基金内：先抓重仓（fund_holdings），再抓行业（fund_industry_allocations），
  按 report_date 先删后插，写入幂等；单只失败仅记录并继续下一只；
- 历史多年度回填通过多次 ``--year`` 运行完成，各年状态行互相独立。
"""

from __future__ import annotations

import time
from datetime import UTC, date, datetime

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.models import (
    CandidatePool,
    CandidatePoolMember,
    FundCatalogEntry,
    FundHolding,
    FundHoldingsSyncStatus,
    FundIndustryAllocation,
    Instrument,
)
from app.services import fund_holdings as holdings_source

DEFAULT_LIMIT = 20
REQUEST_INTERVAL_SECONDS = 0.15


def latest_pool_id(db: Session) -> int | None:
    """最新一期候选池 ID；无候选池时返回 None。"""
    return db.scalar(select(func.max(CandidatePool.id)))


def get_status_row(db: Session, instrument_id: int, year: int) -> FundHoldingsSyncStatus | None:
    return db.scalar(
        select(FundHoldingsSyncStatus).where(
            FundHoldingsSyncStatus.instrument_id == instrument_id,
            FundHoldingsSyncStatus.year == year,
        )
    )


def select_pending(
    db: Session,
    year: int,
    *,
    pool_id: int | None = None,
    codes: list[str] | None = None,
    limit: int | None = None,
) -> list[Instrument]:
    """选出该年度尚未 complete 的候选池成员基金（按池内 rank / 代码排序）。

    未跑过（无状态行）、partial、failed 均视为待处理；complete 跳过。
    pool_id 为 None 时取最新一期候选池；codes 进一步收窄范围。
    """
    if pool_id is None:
        pool_id = latest_pool_id(db)
    stmt = (
        select(Instrument, CandidatePoolMember.rank)
        .join(CandidatePoolMember, CandidatePoolMember.code == Instrument.code)
        .outerjoin(
            FundHoldingsSyncStatus,
            (FundHoldingsSyncStatus.instrument_id == Instrument.id)
            & (FundHoldingsSyncStatus.year == year),
        )
        .where(
            CandidatePoolMember.pool_id == pool_id,
            CandidatePoolMember.status == "active",
            (FundHoldingsSyncStatus.id.is_(None))
            | (~FundHoldingsSyncStatus.status.in_(("complete", "not_applicable"))),
        )
        .order_by(CandidatePoolMember.rank, Instrument.code)
    )
    if codes:
        stmt = stmt.where(Instrument.code.in_(codes))
    if limit:
        stmt = stmt.limit(limit)
    return [row[0] for row in db.execute(stmt).all()]


def _upsert_status(
    db: Session,
    instrument_id: int,
    year: int,
    *,
    status: str,
    holding_rows: int,
    industry_rows: int,
    error: str | None,
) -> None:
    row = get_status_row(db, instrument_id, year)
    if row is None:
        row = FundHoldingsSyncStatus(instrument_id=instrument_id, year=year)
        db.add(row)
    row.status = status
    row.holding_rows = holding_rows
    row.industry_rows = industry_rows
    row.last_error = error
    row.fetched_at = datetime.now(UTC)


def backfill_one(
    db: Session,
    instrument: Instrument,
    year: int,
    *,
    source=holdings_source,
) -> dict:
    """同步单只基金某年度的重仓与行业披露，幂等写入并更新状态行。

    失败抛出的异常由调用方捕获并记录为 failed；本函数自身负责提交。
    """
    holdings = source.fetch_holdings(instrument.code, year)
    industries = source.fetch_industries(instrument.code, year)
    try:
        if holdings:
            report_dates = {item["report_date"] for item in holdings}
            db.execute(
                delete(FundHolding).where(
                    FundHolding.instrument_id == instrument.id,
                    FundHolding.report_date.in_(report_dates),
                )
            )
            for item in holdings:
                db.add(FundHolding(instrument_id=instrument.id, **item))
        if industries:
            report_dates = {item["report_date"] for item in industries}
            db.execute(
                delete(FundIndustryAllocation).where(
                    FundIndustryAllocation.instrument_id == instrument.id,
                    FundIndustryAllocation.report_date.in_(report_dates),
                )
            )
            for item in industries:
                db.add(FundIndustryAllocation(instrument_id=instrument.id, **item))
        # 债券/QDII/货币等非权益基金可能合法地不披露股票重仓；这类空结果
        # 标记为 not_applicable，避免每轮永久重试。权益基金空数据仍记 failed。
        non_equity_tokens = (
            "债券", "纯债", "货币", "现金", "QDII-纯债", "固收", "国债", "信用债"
        )
        catalog_type = db.scalar(
            select(FundCatalogEntry.fund_type).where(FundCatalogEntry.code == instrument.code)
        ) or ""
        non_equity = any(
            token in f"{instrument.name} {catalog_type}" for token in non_equity_tokens
        )
        if not holdings and non_equity:
            status = "not_applicable"
            error = "该基金类型不适用股票重仓披露"
        elif not holdings:
            status = "failed"
            error = "no holdings returned"
        elif not industries:
            status = "partial"
            error = None
        else:
            status = "complete"
            error = None
        _upsert_status(
            db,
            instrument.id,
            year,
            status=status,
            holding_rows=len(holdings),
            industry_rows=len(industries),
            error=error,
        )
        db.commit()
        return {
            "code": instrument.code,
            "status": status,
            "holding_rows": len(holdings),
            "industry_rows": len(industries),
            "error": error,
        }
    except Exception as exc:
        db.rollback()
        _upsert_status(
            db,
            instrument.id,
            year,
            status="failed",
            holding_rows=0,
            industry_rows=0,
            error=str(exc)[:500],
        )
        db.commit()
        return {
            "code": instrument.code,
            "status": "failed",
            "holding_rows": 0,
            "industry_rows": 0,
            "error": str(exc)[:500],
        }


def backfill_fund_holdings(
    db: Session,
    year: int | None = None,
    *,
    pool_id: int | None = None,
    codes: list[str] | None = None,
    limit: int = DEFAULT_LIMIT,
    dry_run: bool = False,
    source=holdings_source,
    sleep_seconds: float = REQUEST_INTERVAL_SECONDS,
) -> dict:
    """候选池披露回填主流程：选出待处理基金，逐只抓取，失败继续。"""
    year = year or date.today().year
    resolved_pool_id = pool_id if pool_id is not None else latest_pool_id(db)
    if resolved_pool_id is None:
        return {
            "year": year,
            "pool_id": None,
            "selected": 0,
            "processed": 0,
            "complete": 0,
            "partial": 0,
            "failed": 0,
            "holding_rows": 0,
            "industry_rows": 0,
            "results": [],
        }

    pending = select_pending(db, year, pool_id=resolved_pool_id, codes=codes, limit=limit)
    results: list[dict] = []
    if dry_run:
        for instrument in pending:
            row = get_status_row(db, instrument.id, year)
            state = row.status if row else "never"
            results.append({"code": instrument.code, "name": instrument.name, "state": state})
        return {
            "year": year,
            "pool_id": resolved_pool_id,
            "selected": len(pending),
            "processed": 0,
            "complete": 0,
            "partial": 0,
            "failed": 0,
            "holding_rows": 0,
            "industry_rows": 0,
            "results": results,
            "dry_run": True,
        }

    for instrument in pending:
        try:
            result = backfill_one(db, instrument, year, source=source)
        except Exception as exc:  # fetch 阶段异常：记录 failed 并继续
            db.rollback()
            _upsert_status(
                db,
                instrument.id,
                year,
                status="failed",
                holding_rows=0,
                industry_rows=0,
                error=str(exc)[:500],
            )
            db.commit()
            result = {
                "code": instrument.code,
                "status": "failed",
                "holding_rows": 0,
                "industry_rows": 0,
                "error": str(exc)[:500],
            }
        results.append(result)
        if sleep_seconds > 0:
            time.sleep(sleep_seconds)

    return {
        "year": year,
        "pool_id": resolved_pool_id,
        "selected": len(pending),
        "processed": len(results),
        "complete": sum(1 for r in results if r["status"] == "complete"),
        "partial": sum(1 for r in results if r["status"] == "partial"),
        "failed": sum(1 for r in results if r["status"] == "failed"),
        "holding_rows": sum(r["holding_rows"] for r in results),
        "industry_rows": sum(r["industry_rows"] for r in results),
        "results": results,
    }
