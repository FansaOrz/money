"""基金详情接口 Schema。"""

from datetime import datetime
from typing import Any

from pydantic import Field

from app.schemas.common import ConfiguredBaseModel, DecimalStr
from app.schemas.quant import FundAdvice


class FundProfileOut(ConfiguredBaseModel):
    code: str
    short_name: str | None = None
    full_name: str | None = None
    inception_date: str | None = None
    latest_scale: str | None = None
    company: str | None = None
    manager: str | None = None
    custodian: str | None = None
    fund_type: str | None = None
    rating_agency: str | None = None
    rating: str | None = None
    investment_objective: str | None = None
    investment_strategy: str | None = None
    benchmark: str | None = None
    management_fee: str | None = None
    custody_fee: str | None = None
    source: str | None = None
    fetched_at: str | None = None


class FundDetailHolding(ConfiguredBaseModel):
    rank: int | None = None
    stock_code: str
    stock_name: str
    weight: DecimalStr
    shares: DecimalStr | None = None
    market_value: DecimalStr | None = None
    report_date: str


class FundDetailIndustry(ConfiguredBaseModel):
    industry: str
    weight: DecimalStr
    market_value: DecimalStr | None = None
    report_date: str


class FundNewsEventOut(ConfiguredBaseModel):
    id: int
    title: str
    summary: str
    direction: str
    impact_level: str
    relation_type: str
    reason: str
    score: float
    published_at: datetime | None = None
    source_count: int
    analysis_method: str


class FundAnalysisSummary(ConfiguredBaseModel):
    """量化、新闻和个人持仓约束合成后的直白说明。"""

    quant_score: int
    news_score: float
    combined_score: int
    quant_view: str
    news_view: str
    portfolio_view: str
    conclusion: str
    conflict_note: str | None = None
    as_of: datetime | None = None
    news_event_count: int = 0
    news_analysis_method: str
    key_events: list[FundNewsEventOut] = Field(default_factory=list)


class FundDetailResponse(ConfiguredBaseModel):
    code: str
    name: str
    fund_type: str | None = None
    market: str | None = None
    family: str | None = None
    share_class: str | None = None
    active: bool | None = None
    profile: FundProfileOut | None = None
    metrics: dict[str, Any] | None = None
    metrics_as_of: str | None = None
    metrics_basis: str | None = None
    advice: FundAdvice | None = None
    analysis: FundAnalysisSummary | None = None
    holdings: list[FundDetailHolding] = Field(default_factory=list)
    industries: list[FundDetailIndustry] = Field(default_factory=list)
    report_date: str | None = None
    warnings: list[str] = Field(default_factory=list)


class FundRefreshResponse(FundDetailResponse):
    refreshed: bool = True
