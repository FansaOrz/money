"""基金成分与组合穿透接口。"""

from collections import defaultdict
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.models import FundHolding, FundIndustryAllocation, Instrument, Position
from app.schemas.holdings import (
    FundCompositionResponse,
    FundHoldingItem,
    FundIndustryItem,
    PortfolioExposureItem,
    PortfolioExposureResponse,
)

router = APIRouter(prefix="/holdings", tags=["holdings"])


@router.get("/{fund_code}", response_model=FundCompositionResponse)
def fund_composition(fund_code: str, db: Session = Depends(get_db)) -> FundCompositionResponse:
    instrument = db.scalar(select(Instrument).where(Instrument.code == fund_code))
    if instrument is None:
        raise HTTPException(404, "基金不存在")
    latest_holding = db.scalar(select(func.max(FundHolding.report_date)).where(FundHolding.instrument_id == instrument.id))
    latest_industry = db.scalar(select(func.max(FundIndustryAllocation.report_date)).where(FundIndustryAllocation.instrument_id == instrument.id))
    holdings = db.scalars(
        select(FundHolding).where(
            FundHolding.instrument_id == instrument.id,
            FundHolding.report_date == latest_holding,
        ).order_by(FundHolding.rank)
    ).all() if latest_holding else []
    industries = db.scalars(
        select(FundIndustryAllocation).where(
            FundIndustryAllocation.instrument_id == instrument.id,
            FundIndustryAllocation.report_date == latest_industry,
        ).order_by(FundIndustryAllocation.weight.desc())
    ).all() if latest_industry else []
    return FundCompositionResponse(
        fund_code=fund_code,
        fund_name=instrument.name,
        holdings=[FundHoldingItem(stock_code=x.stock_code, stock_name=x.stock_name, weight=x.weight, shares=x.shares, market_value=x.market_value, report_date=x.report_date.isoformat()) for x in holdings],
        industries=[FundIndustryItem(industry=x.industry, weight=x.weight, market_value=x.market_value, report_date=x.report_date.isoformat()) for x in industries],
        report_date=max(latest_holding, latest_industry).isoformat() if latest_holding or latest_industry else None,
    )


@router.get("/portfolio/exposure", response_model=PortfolioExposureResponse)
def portfolio_exposure(
    db: Session = Depends(get_db),
    stock_limit: int = Query(default=50, ge=1, le=2000, description="返回的底层股票条数上限"),
    industry_limit: int = Query(default=50, ge=1, le=500, description="返回的行业条数上限"),
) -> PortfolioExposureResponse:
    position_rows = db.execute(
        select(Instrument.id, Instrument.code, func.sum(Position.market_value))
        .join(Position, Position.instrument_id == Instrument.id)
        .group_by(Instrument.id, Instrument.code)
    ).all()
    total = sum((row[2] or Decimal("0")) for row in position_rows)
    stock_exposure: dict[tuple[str, str], dict] = defaultdict(lambda: {"value": Decimal("0"), "funds": set(), "date": None})
    industry_exposure: dict[str, dict] = defaultdict(lambda: {"value": Decimal("0"), "funds": set(), "date": None})
    covered = Decimal("0")
    for instrument_id, fund_code, fund_value in position_rows:
        fund_value = fund_value or Decimal("0")
        latest = db.scalar(select(func.max(FundHolding.report_date)).where(FundHolding.instrument_id == instrument_id))
        holdings = db.scalars(select(FundHolding).where(FundHolding.instrument_id == instrument_id, FundHolding.report_date == latest)).all() if latest else []
        if holdings:
            covered += fund_value
        for item in holdings:
            key = (item.stock_code, item.stock_name)
            stock_exposure[key]["value"] += fund_value * item.weight / Decimal("100")
            stock_exposure[key]["funds"].add(fund_code)
            stock_exposure[key]["date"] = max(stock_exposure[key]["date"], item.report_date) if stock_exposure[key]["date"] else item.report_date
        latest_ind = db.scalar(select(func.max(FundIndustryAllocation.report_date)).where(FundIndustryAllocation.instrument_id == instrument_id))
        industries = db.scalars(select(FundIndustryAllocation).where(FundIndustryAllocation.instrument_id == instrument_id, FundIndustryAllocation.report_date == latest_ind)).all() if latest_ind else []
        for item in industries:
            industry_exposure[item.industry]["value"] += fund_value * item.weight / Decimal("100")
            industry_exposure[item.industry]["funds"].add(fund_code)
            industry_exposure[item.industry]["date"] = max(industry_exposure[item.industry]["date"], item.report_date) if industry_exposure[item.industry]["date"] else item.report_date

    def stock_item(entry):
        (code, name), data = entry
        return PortfolioExposureItem(code=code, name=name, portfolio_weight=data["value"] / total if total else Decimal("0"), source_funds=len(data["funds"]), report_date=data["date"].isoformat() if data["date"] else None)
    def industry_item(entry):
        name, data = entry
        return PortfolioExposureItem(code=name, name=name, portfolio_weight=data["value"] / total if total else Decimal("0"), source_funds=len(data["funds"]), report_date=data["date"].isoformat() if data["date"] else None)
    # 先完整排序，再按 limit 截断；total 字段记录截断前的总数
    stocks_sorted = sorted((stock_item(x) for x in stock_exposure.items()), key=lambda x: x.portfolio_weight, reverse=True)
    industries_sorted = sorted((industry_item(x) for x in industry_exposure.items()), key=lambda x: x.portfolio_weight, reverse=True)
    return PortfolioExposureResponse(
        stocks=stocks_sorted[:stock_limit],
        industries=industries_sorted[:industry_limit],
        stocks_total=len(stocks_sorted),
        industries_total=len(industries_sorted),
        covered_market_value=covered,
        total_market_value=total,
        coverage_rate=covered / total if total else None,
    )
