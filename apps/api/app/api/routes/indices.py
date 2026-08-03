"""主要市场指数路由：摘要列表与历史日线查询。"""

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.schemas.indices import (
    IndexHistoryResponse,
    IndexListResponse,
    IndexQuoteOut,
    IndexSummary,
    IndexSyncResult,
)
from app.services import index_data

router = APIRouter(prefix="/indices", tags=["indices"])


@router.get("", response_model=IndexListResponse)
def list_indices(db: Session = Depends(get_db)) -> IndexListResponse:
    """返回全部跟踪指数的最新行情摘要。"""
    summaries = index_data.list_index_summaries(db)
    return IndexListResponse(
        items=[IndexSummary(**item) for item in summaries],
        total=len(summaries),
    )


@router.get("/{code}/history", response_model=IndexHistoryResponse)
def get_index_history(
    code: str,
    days: int = Query(default=90, ge=1, le=3650, description="最近 N 个交易日"),
    db: Session = Depends(get_db),
) -> IndexHistoryResponse:
    """返回单个指数近 N 个交易日的日线（按日期升序）。"""
    result = index_data.get_index_history(db, code, days=days)
    if result is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"未知指数代码：{code}",
        )
    index, quotes = result
    return IndexHistoryResponse(
        code=index.code,
        name=index.name,
        days=days,
        items=[
            IndexQuoteOut(
                date=quote.trade_date,
                open=quote.open,
                high=quote.high,
                low=quote.low,
                close=quote.close,
                volume=quote.volume,
                change_pct=quote.change_pct,
            )
            for quote in quotes
        ],
    )


@router.post("/sync", response_model=IndexSyncResult, status_code=status.HTTP_200_OK)
def sync_indices(
    days: int = Query(default=30, ge=1, le=3650),
    db: Session = Depends(get_db),
) -> IndexSyncResult:
    """手动触发一次指数行情同步（幂等 upsert）。"""
    return IndexSyncResult(**index_data.sync_index_history(db, days=days))
