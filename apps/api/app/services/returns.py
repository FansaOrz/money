"""组合区间收益服务：今日 / 近一周 / 近一月 / 近三月收益估算。

口径说明：
- 按当前份额估算：收益金额 = shares * (unit_nav_end - unit_nav_start)，
  即单位净值价差口径（不含现金分红，分红以 REINVEST 流水另行入账）；
- 收益率为总收益口径：由单位净值与累计净值识别现金分红，并按除息日净值
  再投资构造复权序列；累计净值缺失区间回退单位净值；
- 注意金额与收益率是两个不同口径：金额按单位净值价差，收益率按分红再投资
  总收益；区间有分红时两者会不同（见 rate_basis 字段区分）；
- 终点为该基金最新一条净值；起点为 <= 目标日期的最后一条净值，
  实际使用的端点日期随结果返回；
- 窗口内存在 BUY/SELL/REINVEST 流水时标记为 approximate（份额在窗口内变动，
  用当前份额估算会失真）；净值端点缺失时标记为 stale 并说明原因；
- QDII 基金净值披露滞后，其“最新净值日期”通常早于境内基金，
  不参与全局终点对齐，按自身最新净值计算并在结果中体现实际日期；
- 组合收益按各基金期末金额加权，coverage 表示参与加权的金额占比。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import FundNav, Instrument, Position, Transaction, TransactionType
from app.schemas.portfolio import (
    FundReturnItem,
    PortfolioReturnsResponse,
    PortfolioReturnWindow,
)

# 窗口标识 -> 目标起点相对终点的偏移
WINDOWS: dict[str, str] = {
    "1d": "今日",
    "1w": "近一周",
    "1m": "近一月",
    "3m": "近三月",
}

# 窗口内出现这些类型的流水则认为份额发生变动
_FLOW_TYPES = (TransactionType.BUY, TransactionType.SELL, TransactionType.REINVEST)

# 净值披露滞后超过该天数时，在 stale_reason 中提示（仅提示，不影响 available 判定）
_QDII_LAG_HINT_DAYS = 3


def _month_shift(day: date, months: int) -> date:
    """将日期向前推若干自然月，月末日期回退到目标月最后一天。"""
    month_index = day.year * 12 + (day.month - 1) - months
    year, month_zero = divmod(month_index, 12)
    month = month_zero + 1
    if month == 12:
        next_month_first = date(year + 1, 1, 1)
    else:
        next_month_first = date(year, month + 1, 1)
    last_day = (next_month_first - timedelta(days=1)).day
    return date(year, month, min(day.day, last_day))


def _target_start(window: str, end: date) -> date:
    """各窗口的目标起点日期。"""
    if window == "1d":
        return end - timedelta(days=1)
    if window == "1w":
        return end - timedelta(days=7)
    if window == "1m":
        return _month_shift(end, 1)
    if window == "3m":
        return _month_shift(end, 3)
    raise ValueError(f"不支持的窗口: {window}")


def _is_qdii(name: str) -> bool:
    """按基金名称识别 QDII。"""
    return "QDII" in name.upper()


@dataclass(frozen=True)
class _NavPoint:
    nav_date: date
    unit_nav: Decimal
    accumulated_nav: Decimal | None


def _latest_nav(db: Session, instrument_id: int) -> _NavPoint | None:
    row = db.execute(
        select(FundNav.nav_date, FundNav.unit_nav, FundNav.accumulated_nav)
        .where(FundNav.instrument_id == instrument_id)
        .order_by(FundNav.nav_date.desc())
        .limit(1)
    ).first()
    if row is None:
        return None
    return _NavPoint(nav_date=row[0], unit_nav=row[1], accumulated_nav=row[2])


def _nav_on_or_before(db: Session, instrument_id: int, target: date) -> _NavPoint | None:
    """<= 目标日期的最后一条净值。"""
    row = db.execute(
        select(FundNav.nav_date, FundNav.unit_nav, FundNav.accumulated_nav)
        .where(
            FundNav.instrument_id == instrument_id,
            FundNav.nav_date <= target,
        )
        .order_by(FundNav.nav_date.desc())
        .limit(1)
    ).first()
    if row is None:
        return None
    return _NavPoint(nav_date=row[0], unit_nav=row[1], accumulated_nav=row[2])


def _previous_nav(db: Session, instrument_id: int, end_date: date) -> _NavPoint | None:
    """取该基金最新净值之前的上一期净值。

    支付宝“日收益”按每只基金本次公布净值相对上次公布净值计算；QDII、FOF
    的净值日期通常落后，不能统一拿全局 T-1 日期，否则会把它们错误算成 0。
    """
    row = db.execute(
        select(FundNav.nav_date, FundNav.unit_nav, FundNav.accumulated_nav)
        .where(
            FundNav.instrument_id == instrument_id,
            FundNav.nav_date < end_date,
        )
        .order_by(FundNav.nav_date.desc())
        .limit(1)
    ).first()
    if row is None:
        return None
    return _NavPoint(nav_date=row[0], unit_nav=row[1], accumulated_nav=row[2])


def _has_flows(db: Session, instrument_id: int, start: date, end: date) -> bool:
    """窗口 (start, end] 内是否存在份额变动流水。"""
    count = db.scalar(
        select(func.count(Transaction.id)).where(
            Transaction.instrument_id == instrument_id,
            Transaction.type.in_(_FLOW_TYPES),
            Transaction.trade_date > start,
            Transaction.trade_date <= end,
        )
    )
    return bool(count)


def _return_rate(
    db: Session,
    instrument_id: int,
    start: _NavPoint,
    end: _NavPoint,
) -> tuple[Decimal | None, str | None]:
    """收益率与其口径：现金分红再投资总收益，缺测区间回退单位净值。"""
    from app.services.quant import _load_dual_nav_series

    pair = _load_dual_nav_series(
        db,
        instrument_id,
        start=start.nav_date,
        end=end.nav_date,
        limit=2000,
    )
    if len(pair.total_series) < 2 or pair.total_values[0] <= 0:
        return None, None
    value = pair.total_values[-1] / pair.total_values[0] - 1.0
    basis = "total_return_with_unit_fallback" if pair.unit_fallback else "dividend_reinvested"
    return Decimal(str(value)), basis


def _fund_return_item(
    db: Session,
    instrument: Instrument,
    shares: Decimal,
    target_start: date,
    reference_end: date,
) -> FundReturnItem:
    """计算单只基金在一个窗口内的收益条目。"""
    is_qdii = _is_qdii(instrument.name)
    end = _latest_nav(db, instrument.id)
    if end is None:
        return FundReturnItem(
            instrument_id=instrument.id,
            instrument_code=instrument.code,
            instrument_name=instrument.name,
            is_qdii=is_qdii,
            shares=shares,
            return_amount=None,
            return_rate=None,
            start_date=None,
            end_date=None,
            status="stale",
            stale_reason="无净值数据",
            has_flows=False,
            weight=None,
        )

    # “今日”按基金各自最近两个净值日计算；其他窗口按目标自然日起点计算。
    start = (
        _previous_nav(db, instrument.id, end.nav_date)
        if target_start == reference_end - timedelta(days=1)
        else _nav_on_or_before(db, instrument.id, target_start)
    )
    has_flows = _has_flows(db, instrument.id, target_start, end.nav_date)

    if start is None:
        reason = f"起点 {target_start.isoformat()} 之前无净值数据"
        if is_qdii:
            reason += "；QDII 净值披露滞后，历史回填可能不完整"
        return FundReturnItem(
            instrument_id=instrument.id,
            instrument_code=instrument.code,
            instrument_name=instrument.name,
            is_qdii=is_qdii,
            shares=shares,
            return_amount=None,
            return_rate=None,
            start_date=None,
            end_date=end.nav_date.isoformat(),
            status="stale",
            stale_reason=reason,
            has_flows=has_flows,
            weight=None,
        )

    return_amount = shares * (end.unit_nav - start.unit_nav)
    rate, basis = _return_rate(db, instrument.id, start, end)
    status = "approximate" if has_flows else "available"

    stale_reason = None
    if is_qdii and (reference_end - end.nav_date).days > _QDII_LAG_HINT_DAYS:
        lag = (reference_end - end.nav_date).days
        stale_reason = f"QDII 净值披露滞后，最新净值为 {lag} 天前"

    return FundReturnItem(
        instrument_id=instrument.id,
        instrument_code=instrument.code,
        instrument_name=instrument.name,
        is_qdii=is_qdii,
        shares=shares,
        return_amount=return_amount,
        return_rate=rate,
        start_date=start.nav_date.isoformat(),
        end_date=end.nav_date.isoformat(),
        start_nav=start.unit_nav,
        end_nav=end.unit_nav,
        rate_basis=basis,
        status=status,
        stale_reason=stale_reason,
        has_flows=has_flows,
        weight=None,
    )


def _compute_window(
    db: Session,
    window: str,
    holdings: list[tuple[Instrument, Decimal]],
    reference_end: date,
) -> PortfolioReturnWindow:
    """计算单个窗口的组合收益。"""
    target_start = _target_start(window, reference_end)
    items = [
        _fund_return_item(db, instrument, shares, target_start, reference_end)
        for instrument, shares in holdings
    ]

    # 期末金额（权重与 coverage 基准）：有净值的基金按最新净值估值
    end_values: dict[int, Decimal] = {}
    for item in items:
        if item.end_nav is not None:
            end_values[item.instrument_id] = item.shares * item.end_nav
    total_end_value = sum(end_values.values(), Decimal("0"))

    # 组合加权：仅纳入 status 为 available / approximate 的基金
    total_return = Decimal("0")
    total_base = Decimal("0")
    weighted_end_value = Decimal("0")
    as_of_end: date | None = None
    available_count = approximate_count = stale_count = 0
    for item in items:
        if item.status == "stale":
            stale_count += 1
            continue
        if item.status == "approximate":
            approximate_count += 1
        else:
            available_count += 1
        end_value = end_values[item.instrument_id]
        weighted_end_value += end_value
        if item.return_amount is not None:
            total_return += item.return_amount
            total_base += end_value - item.return_amount
        if item.end_date is not None:
            end_date = date.fromisoformat(item.end_date)
            as_of_end = max(as_of_end, end_date) if as_of_end else end_date

    for item in items:
        if item.instrument_id in end_values and total_end_value > 0:
            item.weight = end_values[item.instrument_id] / total_end_value

    portfolio_rate = (total_return / total_base) if total_base > 0 else None
    coverage = (weighted_end_value / total_end_value) if total_end_value > 0 else Decimal("0")

    return PortfolioReturnWindow(
        window=window,
        target_start_date=target_start.isoformat(),
        return_amount=total_return if (available_count + approximate_count) > 0 else None,
        return_rate=portfolio_rate,
        coverage=coverage,
        available_count=available_count,
        approximate_count=approximate_count,
        stale_count=stale_count,
        as_of_end_date=as_of_end.isoformat() if as_of_end else None,
        items=items,
    )


def get_portfolio_returns(
    db: Session, windows: list[str] | None = None
) -> PortfolioReturnsResponse:
    """组合区间收益：默认一次返回 1d / 1w / 1m / 3m 四个窗口。

    windows 指定时只返回请求的窗口（单窗口查询）。
    """
    selected = windows or list(WINDOWS)
    invalid = [w for w in selected if w not in WINDOWS]
    if invalid:
        raise ValueError(f"不支持的窗口: {', '.join(invalid)}")

    # 按基金聚合当前份额（多账户合并）
    rows = db.execute(
        select(Instrument, func.sum(Position.shares))
        .join(Instrument, Position.instrument_id == Instrument.id)
        .group_by(Instrument.id)
        .order_by(Instrument.id)
    ).all()
    holdings: list[tuple[Instrument, Decimal]] = [
        (instrument, shares) for instrument, shares in rows if shares and shares > 0
    ]

    if not holdings:
        return PortfolioReturnsResponse(windows={})

    # 全局参考终点：所有持仓基金中的最新净值日期。
    # QDII 净值滞后，不参与对齐；各基金仍使用自身最新净值作为终点。
    reference_end = db.scalar(select(func.max(FundNav.nav_date))) or date.today()

    result = {
        window: _compute_window(db, window, holdings, reference_end) for window in selected
    }
    return PortfolioReturnsResponse(windows=result)
