"""A 股规则策略两个月前向模拟 API。"""

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.schemas.stock_paper import (
    StockPaperRunResponse,
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


@router.get("/trades", response_model=list[StockPaperTradeOut])
def trades(
    limit: int = Query(default=500, ge=1, le=2000),
    db: Session = Depends(get_db),
) -> list[StockPaperTradeOut]:
    """股票模拟成交明细。"""
    return [StockPaperTradeOut(**item) for item in stock_paper.list_trades(db, limit)]
