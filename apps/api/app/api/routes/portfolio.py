"""组合相关路由：汇总与持仓列表。"""

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.db.session import get_db
from sqlalchemy import select

from app.models import PortfolioSnapshot
from app.schemas.portfolio import (
    NavSyncResult,
    PortfolioReturnsResponse,
    PortfolioSnapshotItem,
    PortfolioSummary,
    PositionItem,
    PositionListResponse,
    SeedPositionRequest,
)
from app.services import portfolio as portfolio_service
from app.services import returns as returns_service
from app.services.fund_data import sync_fund_navs

router = APIRouter(prefix="/portfolio", tags=["portfolio"])


@router.get("/returns", response_model=PortfolioReturnsResponse)
def portfolio_returns(
    window: str | None = Query(
        default=None,
        description="单窗口查询：1d / 1w / 1m / 3m；缺省时一次返回全部窗口",
    ),
    db: Session = Depends(get_db),
) -> PortfolioReturnsResponse:
    """组合区间收益：今日 / 近一周 / 近一月 / 近三月。

    按当前份额估算；组合按各基金期末金额加权，并返回数据覆盖率。
    """
    windows = [w.strip() for w in window.split(",") if w.strip()] if window else None
    try:
        return returns_service.get_portfolio_returns(db, windows)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


@router.get("/summary", response_model=PortfolioSummary)
def portfolio_summary(db: Session = Depends(get_db)) -> PortfolioSummary:
    """组合整体汇总：总成本、总市值、总盈亏、收益率。"""
    return portfolio_service.get_summary(db)


@router.get("/positions", response_model=PositionListResponse)
def portfolio_positions(db: Session = Depends(get_db)) -> PositionListResponse:
    """全部持仓列表。"""
    return portfolio_service.list_positions(db)


@router.post("/sync-navs", response_model=NavSyncResult)
def sync_navs(db: Session = Depends(get_db)) -> NavSyncResult:
    """优先同步当前持仓基金最新净值，并生成组合资产快照。"""
    return NavSyncResult(**sync_fund_navs(db, held_only=True))


@router.get("/snapshots", response_model=list[PortfolioSnapshotItem])
def portfolio_snapshots(db: Session = Depends(get_db)) -> list[PortfolioSnapshotItem]:
    """组合历史资产快照，用于收益曲线。"""
    snapshots = db.scalars(
        select(PortfolioSnapshot).order_by(PortfolioSnapshot.snapshot_date)
    ).all()
    return [
        PortfolioSnapshotItem(
            snapshot_date=item.snapshot_date.isoformat(),
            total_cost=item.total_cost,
            total_market_value=item.total_market_value,
            total_profit=item.total_profit,
        )
        for item in snapshots
    ]


@router.post(
    "/positions",
    response_model=PositionItem,
    status_code=status.HTTP_201_CREATED,
)
def create_position(
    payload: SeedPositionRequest,
    db: Session = Depends(get_db),
) -> PositionItem:
    """手工录入持仓（开发联调临时接口，后续由对账单导入替代）。"""
    return portfolio_service.seed_position(db, payload)
