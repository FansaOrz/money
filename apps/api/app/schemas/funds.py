"""基金详情接口 Schema。"""

from typing import Any

from pydantic import Field

from app.schemas.common import ConfiguredBaseModel, DecimalStr


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
    holdings: list[FundDetailHolding] = Field(default_factory=list)
    industries: list[FundDetailIndustry] = Field(default_factory=list)
    report_date: str | None = None
    warnings: list[str] = Field(default_factory=list)


class FundRefreshResponse(FundDetailResponse):
    refreshed: bool = True
