"""基金成分和组合穿透响应。"""

from app.schemas.common import ConfiguredBaseModel, DecimalStr


class FundHoldingItem(ConfiguredBaseModel):
    stock_code: str
    stock_name: str
    weight: DecimalStr
    shares: DecimalStr | None
    market_value: DecimalStr | None
    report_date: str


class FundIndustryItem(ConfiguredBaseModel):
    industry: str
    weight: DecimalStr
    market_value: DecimalStr | None
    report_date: str


class FundCompositionResponse(ConfiguredBaseModel):
    fund_code: str
    fund_name: str
    holdings: list[FundHoldingItem]
    industries: list[FundIndustryItem]
    report_date: str | None


class PortfolioExposureItem(ConfiguredBaseModel):
    code: str
    name: str
    portfolio_weight: DecimalStr
    source_funds: int
    report_date: str | None


class PortfolioExposureResponse(ConfiguredBaseModel):
    stocks: list[PortfolioExposureItem]
    industries: list[PortfolioExposureItem]
    # 截断前的完整条目数，供前端展示“已展示 X / 总计 Y”
    stocks_total: int
    industries_total: int
    covered_market_value: DecimalStr
    total_market_value: DecimalStr
    coverage_rate: DecimalStr | None
