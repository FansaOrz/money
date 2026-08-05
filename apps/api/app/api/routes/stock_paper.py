"""A 股规则策略两个月前向模拟 API。"""

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.schemas.stock_paper import (
    StockPaperCancelRequest,
    StockPaperPrepareRequest,
    StockPaperPrepareResponse,
    StockPaperRunResponse,
    StockPaperSignalOut,
    StockPaperSummary,
    StockPaperTradeOut,
)
from app.services import stock_paper

router = APIRouter(prefix="/stocks/paper", tags=["stock-paper"])


@router.get("/summary", response_model=StockPaperSummary)
def summary(db: Session = Depends(get_db)) -> StockPaperSummary:
    """数据就绪度、两个月进度、持仓、净值、基准与阶段指标。"""
    return stock_paper.get_summary(db)


@router.post("/run", response_model=StockPaperRunResponse)
def run(db: Session = Depends(get_db)) -> StockPaperRunResponse:
    """推进到最新真实行情日；同一数据日重复调用幂等。"""
    try:
        return stock_paper.run_cycle(db)
    except stock_paper.StockPaperError as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc


@router.post("/prepare", response_model=StockPaperPrepareResponse)
def prepare(
    payload: StockPaperPrepareRequest,
    db: Session = Depends(get_db),
) -> StockPaperPrepareResponse:
    """执行历史走步/完全留出验证，通过后创建全新的空前向账户。"""
    try:
        return StockPaperPrepareResponse(
            **stock_paper.prepare_forward_account(
                db,
                start=payload.start_date,
                end=payload.end_date,
                top_n_grid=payload.top_n_grid,
                max_stock_weight_grid=payload.max_stock_weight_grid,
                embargo_days=payload.embargo_days,
                create_new_version=payload.create_new_version,
            )
        )
    except stock_paper.StockPaperError as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc


@router.get("/trades", response_model=list[StockPaperTradeOut])
def trades(
    limit: int = Query(default=500, ge=1, le=2000),
    db: Session = Depends(get_db),
) -> list[StockPaperTradeOut]:
    """股票模拟成交明细。"""
    return [StockPaperTradeOut(**item) for item in stock_paper.list_trades(db, limit)]


@router.post(
    "/signals/{signal_id}/cancel",
    response_model=StockPaperSignalOut,
)
def cancel_signal(
    signal_id: int,
    payload: StockPaperCancelRequest,
    db: Session = Depends(get_db),
) -> StockPaperSignalOut:
    """人工撤销尚未完成的模拟调仓信号，并保存机会成本审计。"""
    try:
        return stock_paper.cancel_pending_signal(
            db,
            signal_id,
            reason=payload.reason,
        )
    except stock_paper.StockPaperError as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc
