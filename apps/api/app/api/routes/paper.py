"""模拟交易（Paper Trading）路由。

全部为模拟交易能力：默认账户初始 100 万元、每 20 个交易日调仓、
双边简化费用 0.1%；到调仓日按 screener top10 target_weight 虚拟成交，
按当日 FundNav 估值，候选池等权基准对照。不涉及任何真实下单。
"""

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.schemas.paper import (
    PaperHoldingDayItem,
    PaperHistoryResponse,
    PaperPositionsResponse,
    PaperRunRequest,
    PaperRunResponse,
    PaperSignalsResponse,
    PaperSummary,
    PaperTradesResponse,
)
from app.services import paper as paper_service
from app.services.paper import PaperError
from app.services.quant import QuantError, _parse_day

router = APIRouter(prefix="/paper", tags=["paper"])


@router.get("/summary", response_model=PaperSummary)
def paper_summary(db: Session = Depends(get_db)) -> PaperSummary:
    """模拟账户摘要：最新净值、累计/年化收益、回撤、夏普、基准对照与费用统计。"""
    try:
        return paper_service.get_summary(db)
    except PaperError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc


@router.get("/history", response_model=PaperHistoryResponse)
def paper_history(
    start_date: str | None = Query(default=None, description="起始日 YYYY-MM-DD"),
    end_date: str | None = Query(default=None, description="截止日 YYYY-MM-DD"),
    limit: int = Query(default=500, ge=1, le=5000, description="返回的最大条数"),
    db: Session = Depends(get_db),
) -> PaperHistoryResponse:
    """每日净值历史：策略净值、日收益、累计收益与候选池等权基准。"""
    try:
        return paper_service.get_history(
            db, start_date=start_date, end_date=end_date, limit=limit
        )
    except PaperError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


@router.get("/positions", response_model=PaperPositionsResponse)
def paper_positions(db: Session = Depends(get_db)) -> PaperPositionsResponse:
    """当前持仓：份额、成本、最新市值、权重与浮动盈亏。"""
    try:
        return paper_service.get_positions(db)
    except PaperError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc


@router.get("/holdings", response_model=list[PaperHoldingDayItem])
def paper_holding_history(
    start_date: str | None = Query(default=None, description="起始日 YYYY-MM-DD"),
    end_date: str | None = Query(default=None, description="截止日 YYYY-MM-DD"),
    limit: int = Query(default=2000, ge=1, le=10000, description="返回的最大条数"),
    db: Session = Depends(get_db),
) -> list[PaperHoldingDayItem]:
    """每日持仓估值快照（日期升序）。"""
    try:
        return paper_service.get_holding_history(
            db, start_date=start_date, end_date=end_date, limit=limit
        )
    except PaperError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


@router.get("/trades", response_model=PaperTradesResponse)
def paper_trades(
    limit: int = Query(default=200, ge=1, le=2000, description="返回的最大条数"),
    offset: int = Query(default=0, ge=0, description="分页偏移"),
    db: Session = Depends(get_db),
) -> PaperTradesResponse:
    """虚拟成交记录（日期倒序）：买卖方向、份额、价格、金额、费用与目标权重。"""
    try:
        return paper_service.get_trades(db, limit=limit, offset=offset)
    except PaperError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc


@router.get("/signals", response_model=PaperSignalsResponse)
def paper_signals(
    limit: int = Query(default=60, ge=1, le=500, description="返回的最大条数"),
    offset: int = Query(default=0, ge=0, description="分页偏移"),
    db: Session = Depends(get_db),
) -> PaperSignalsResponse:
    """调仓日固化的全候选信号快照（日期倒序）。"""
    try:
        return paper_service.get_signals(db, limit=limit, offset=offset)
    except PaperError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc


@router.post("/run", response_model=PaperRunResponse)
def paper_run(
    payload: PaperRunRequest | None = None, db: Session = Depends(get_db)
) -> PaperRunResponse:
    """手动触发一次每日模拟交易循环（幂等：同日重跑直接返回已有结果）。

    流程：调用 screener 取全候选信号；到调仓日（每 20 个交易日一次，
    首次运行为建仓日）固化信号快照并按 top10 target_weight 虚拟成交；
    按当日 FundNav 估值落库；生成候选池等权基准。不产生任何真实下单。
    """
    run_date_str = payload.run_date if payload else None
    try:
        run_date = _parse_day(run_date_str)
    except QuantError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from exc
    try:
        return paper_service.run_paper_cycle(db, run_date=run_date)
    except PaperError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
