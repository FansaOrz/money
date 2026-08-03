"""持仓查询接口，返回前端直接使用的一维数组。"""

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.models import FundNav, Instrument, Position, Transaction
from app.schemas.imports import FundNavHistoryItem, FundNavHistoryResponse, FundTradePoint, PreviewPosition

router = APIRouter(prefix="/positions", tags=["positions"])


@router.get("/{fund_code}/nav-history", response_model=FundNavHistoryResponse)
def fund_nav_history(fund_code: str, db: Session = Depends(get_db)) -> FundNavHistoryResponse:
    """返回单只基金的历史净值走势。"""
    instrument = db.scalar(select(Instrument).where(Instrument.code == fund_code))
    if instrument is None:
        return FundNavHistoryResponse(fund_code=fund_code, fund_name="未知基金", items=[], total=0)
    rows = db.scalars(
        select(FundNav)
        .where(FundNav.instrument_id == instrument.id)
        .order_by(FundNav.nav_date)
    ).all()
    trades = db.scalars(
        select(Transaction)
        .where(Transaction.instrument_id == instrument.id)
        .order_by(Transaction.trade_date, Transaction.id)
    ).all()
    return FundNavHistoryResponse(
        fund_code=instrument.code,
        fund_name=instrument.name,
        items=[
            FundNavHistoryItem(
                nav_date=row.nav_date.isoformat(),
                unit_nav=row.unit_nav,
                accumulated_nav=row.accumulated_nav,
                daily_growth_rate=row.daily_growth_rate,
            )
            for row in rows
        ],
        trades=[
            FundTradePoint(
                trade_date=trade.trade_date.isoformat(),
                type=trade.type.value,
                amount=trade.amount,
                shares=trade.shares,
            )
            for trade in trades
        ],
        total=len(rows),
    )


@router.get("", response_model=list[PreviewPosition])
def list_positions(db: Session = Depends(get_db)) -> list[PreviewPosition]:
    rows = db.execute(
        select(
            Instrument.code,
            Instrument.name,
            func.sum(Position.shares),
            func.sum(Position.cost),
            func.sum(Position.market_value),
            func.max(Position.latest_nav),
            func.max(Position.nav_date),
        )
        .join(Instrument, Position.instrument_id == Instrument.id)
        .group_by(Instrument.id, Instrument.code, Instrument.name)
        .order_by(func.sum(Position.market_value).desc().nullslast())
    ).all()
    items = []
    for code, name, shares, cost, market_value, nav, nav_date in rows:
        # 交易流水没有覆盖全部历史基金。成本为 0 时不展示虚假的“市值即收益”。
        profit_available = cost is not None and cost > 0
        cost_price = None if not profit_available or shares == 0 else cost / shares
        profit = None if not profit_available or market_value is None else market_value - cost
        return_rate = None if profit is None or cost == 0 else profit / cost
        coverage_rate = None if market_value in (None, 0) else min(cost / market_value, 1) if profit_available else None
        items.append(
            PreviewPosition(
                fund_code=code,
                fund_name=name,
                shares=shares,
                nav=nav,
                nav_date=nav_date.isoformat() if nav_date else None,
                market_value=market_value,
                cost_price=cost_price,
                profit=profit,
                return_rate=return_rate,
                profit_available=profit_available,
                cost_coverage_rate=coverage_rate,
            )
        )
    return items
