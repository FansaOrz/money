"""组合服务：汇总与持仓列表的基础实现。

当前逻辑：基于 positions 表静态汇总（无实时行情）。
后续接入行情服务后，market_value 将由最新净值计算。
所有金额运算使用 Decimal，保证精度。
"""

from decimal import Decimal

from sqlalchemy import extract, func, select
from sqlalchemy.orm import Session

from app.models import Account, Instrument, PerformanceBaseline, Position, Transaction, TransactionType
from app.schemas.portfolio import (
    PortfolioSummary,
    PositionItem,
    PositionListResponse,
    SeedPositionRequest,
)


def _profit_of(position: Position) -> Decimal | None:
    """单个持仓的浮动盈亏；无市值信息时返回 None。"""
    if position.market_value is None:
        return None
    return position.market_value - position.cost


def _profit_rate_of(profit: Decimal | None, cost: Decimal) -> Decimal | None:
    """收益率 = 盈亏 / 成本；成本为 0 时返回 None。"""
    if profit is None or cost == 0:
        return None
    return profit / cost


def _position_item(position: Position, account: Account, instrument: Instrument) -> PositionItem:
    profit = _profit_of(position)
    cost_price = None if position.shares == 0 else position.cost / position.shares
    return PositionItem(
        account_id=account.id,
        account_name=account.name,
        instrument_id=instrument.id,
        instrument_code=instrument.code,
        instrument_name=instrument.name,
        shares=position.shares,
        cost=position.cost,
        cost_price=cost_price,
        nav=position.latest_nav,
        nav_date=position.nav_date.isoformat() if position.nav_date else None,
        market_value=position.market_value,
        profit=profit,
        profit_rate=_profit_rate_of(profit, position.cost),
    )


def list_positions(db: Session) -> PositionListResponse:
    """按基金聚合多账户持仓，便于展示整体组合。"""
    rows = db.execute(
        select(
            Instrument,
            func.sum(Position.shares),
            func.sum(Position.cost),
            func.sum(Position.market_value),
            func.max(Position.latest_nav),
            func.max(Position.nav_date),
        )
        .join(Instrument, Position.instrument_id == Instrument.id)
        .group_by(Instrument.id)
        .order_by(Instrument.id)
    ).all()
    items: list[PositionItem] = []
    for instrument, shares, cost, market_value, nav, nav_date in rows:
        profit = None if market_value is None else market_value - cost
        items.append(
            PositionItem(
                account_id=0,
                account_name="全部账户",
                instrument_id=instrument.id,
                instrument_code=instrument.code,
                instrument_name=instrument.name,
                shares=shares,
                cost=cost,
                cost_price=None if shares == 0 else cost / shares,
                nav=nav,
                nav_date=nav_date.isoformat() if nav_date else None,
                market_value=market_value,
                profit=profit,
                profit_rate=_profit_rate_of(profit, cost),
            )
        )
    return PositionListResponse(items=items, total=len(items))


def _cash_flow_by_year(db: Session, year: int) -> Decimal:
    buy = db.scalar(
        select(func.sum(Transaction.amount)).where(
            Transaction.type == TransactionType.BUY,
            extract("year", Transaction.trade_date) == year,
        )
    ) or Decimal("0")
    proceeds = db.scalar(
        select(func.sum(Transaction.amount)).where(
            Transaction.type.in_([TransactionType.SELL, TransactionType.DIVIDEND]),
            extract("year", Transaction.trade_date) == year,
        )
    ) or Decimal("0")
    return proceeds - buy


def get_summary(db: Session) -> PortfolioSummary:
    """组合整体汇总：总成本、总市值、总盈亏与整体收益率。"""
    positions = list_positions(db)
    total_cost = Decimal("0")
    total_market_value = Decimal("0")
    snapshot_date = None
    for item in positions.items:
        total_cost += item.cost
        # 无市值信息时回退为成本，保证汇总结果可用
        total_market_value += item.market_value if item.market_value is not None else item.cost
        if item.nav_date is not None:
            snapshot_date = max(snapshot_date, item.nav_date) if snapshot_date else item.nav_date
    estimated_return = total_market_value - total_cost
    estimated_return_rate = _profit_rate_of(estimated_return, total_cost)
    baseline = db.scalar(
        select(PerformanceBaseline).order_by(PerformanceBaseline.as_of_date.desc()).limit(1)
    )
    if baseline is not None:
        # 无新增申购/赎回时，净值同步造成的市值变化就是基准后的收益变化。
        # 若后续发生新交易，需要把交易现金流从该变化中剔除后再更新基准。
        value_change = total_market_value - baseline.market_value
        cumulative_profit = baseline.cumulative_profit + value_change
        current_year_profit = baseline.current_year_profit + value_change
        previous_year_profit = baseline.previous_year_profit
        profit_rate = _profit_rate_of(cumulative_profit, total_market_value - cumulative_profit)
    else:
        cumulative_profit = estimated_return
        current_year_profit = None
        previous_year_profit = None
        profit_rate = estimated_return_rate
    return PortfolioSummary(
        total_cost=total_cost,
        total_market_value=total_market_value,
        total_profit=cumulative_profit,
        profit_rate=profit_rate,
        total_return_rate=profit_rate,
        snapshot_date=snapshot_date,
        estimated_return=estimated_return,
        estimated_return_rate=estimated_return_rate,
        year_return=current_year_profit,
        previous_year_return=previous_year_profit,
        position_count=positions.total,
    )


def seed_position(db: Session, payload: SeedPositionRequest) -> PositionItem:
    """手工录入一条持仓（开发联调用）。

    按名称/代码复用已有账户与标的；同账户同标的的持仓做累加。
    """
    account = db.scalar(select(Account).where(Account.name == payload.account_name))
    if account is None:
        account = Account(name=payload.account_name)
        db.add(account)
        db.flush()

    instrument = db.scalar(
        select(Instrument).where(Instrument.code == payload.instrument_code)
    )
    if instrument is None:
        instrument = Instrument(code=payload.instrument_code, name=payload.instrument_name)
        db.add(instrument)
        db.flush()

    position = db.scalar(
        select(Position).where(
            Position.account_id == account.id,
            Position.instrument_id == instrument.id,
        )
    )
    if position is None:
        position = Position(
            account_id=account.id,
            instrument_id=instrument.id,
            shares=payload.shares,
            cost=payload.cost,
            market_value=payload.market_value,
        )
        db.add(position)
    else:
        position.shares += payload.shares
        position.cost += payload.cost
        if payload.market_value is not None:
            current = position.market_value or Decimal("0")
            position.market_value = current + payload.market_value

    db.commit()
    db.refresh(position)

    return _position_item(position, account, instrument)
