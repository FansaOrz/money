"""A 股研究数据层 Schema。"""

from datetime import date, datetime
from typing import Any

from pydantic import Field

from app.schemas.common import ConfiguredBaseModel


class SyncTaskState(ConfiguredBaseModel):
    """单个同步任务的状态。"""

    status: str = Field(description="never_run | running | success | partial | failed | paused")
    started_at: datetime | None = None
    finished_at: datetime | None = None
    total: int = 0
    updated: int = 0
    failed: int = 0
    last_code: str | None = None
    detail: str | None = None


class MasterStatus(ConfiguredBaseModel):
    stocks: int = 0
    sync: SyncTaskState


class DailyStatus(ConfiguredBaseModel):
    stocks_tracked: int = 0
    stocks_with_parquet: int = 0
    stocks_with_error: int = 0
    first_trade_date: date | None = None
    last_trade_date: date | None = None
    sync: SyncTaskState


class UniverseStatus(ConfiguredBaseModel):
    constituents: dict[str, int] = Field(default_factory=dict, description="指数代码 -> 当前成分数")
    membership_events: int = 0
    snapshots: int = 0
    sync: SyncTaskState


class FundamentalsStatus(ConfiguredBaseModel):
    financial_indicator_rows: int = 0
    financial_indicator_stocks: int = 0
    disclosure_rows: int = 0
    disclosure_stocks: int = 0
    valuation_rows: int = 0
    valuation_stocks: int = 0
    name_history_rows: int = 0
    name_history_stocks: int = 0
    sync_financial: SyncTaskState
    sync_disclosure: SyncTaskState
    sync_valuation: SyncTaskState
    sync_name_history: SyncTaskState


class IndustryStatus(ConfiguredBaseModel):
    stocks: int = 0
    sources: dict[str, dict[str, int]] = Field(default_factory=dict, description="源 -> rows/stocks")
    sync: SyncTaskState


class StockDataStatusResponse(ConfiguredBaseModel):
    """研究数据层 coverage 汇总（真实统计，不估算）。"""

    generated_at: datetime
    master: MasterStatus
    daily: DailyStatus
    universe: UniverseStatus
    fundamentals: FundamentalsStatus
    industry: IndustryStatus


class StockSyncResult(ConfiguredBaseModel):
    """同步任务结果（master / daily / universe / financial / disclosure / valuation / name_history / industry）。"""

    task: str
    status: str = Field(description="success | partial | failed | paused")
    total: int = 0
    updated: int = 0
    failed: int = 0
    rows: int = 0
    stocks: int = 0
    last_code: str | None = None
    errors: list[str] = Field(default_factory=list)


class UniverseMember(ConfiguredBaseModel):
    stock_code: str
    stock_name: str | None = None


class UniverseResponse(ConfiguredBaseModel):
    """某指数某日期成分。basis 标明成分来源可信度。"""

    index_code: str
    index_name: str | None = None
    as_of: date
    basis: str = Field(description="current | snapshot | replay")
    members: list[UniverseMember]
    total: int = 0


class MembershipImportResult(ConfiguredBaseModel):
    status: str
    imported: int = 0
    skipped: int = 0
    errors: list[str] = Field(default_factory=list)


class DailyBarOut(ConfiguredBaseModel):
    """单日日线（从 Parquet 数据湖读出）。"""

    trade_date: date
    open: float | None = None
    high: float | None = None
    low: float | None = None
    close: float | None = None
    volume: int | None = None
    amount: float | None = None
    outstanding_share: float | None = None
    turnover: float | None = None


class StockDailyResponse(ConfiguredBaseModel):
    code: str
    layer: str = Field(description="raw | qfq")
    items: list[DailyBarOut]
    total: int = 0


class StockTechnicalIndicators(ConfiguredBaseModel):
    close: float | None = None
    ma5: float | None = None
    ma10: float | None = None
    ma20: float | None = None
    ma60: float | None = None
    macd_dif: float | None = None
    macd_dea: float | None = None
    macd_histogram: float | None = None
    rsi6: float | None = None
    rsi12: float | None = None
    rsi24: float | None = None
    kdj_k: float | None = None
    kdj_d: float | None = None
    kdj_j: float | None = None
    boll_upper: float | None = None
    boll_middle: float | None = None
    boll_lower: float | None = None
    atr14: float | None = None
    atr_pct: float | None = None
    support20: float | None = None
    resistance20: float | None = None
    volume_ratio: float | None = None


class StockTechnicalResponse(ConfiguredBaseModel):
    code: str
    as_of: date | None = None
    sufficient: bool = False
    sample_size: int = 0
    trend: str = Field(
        description="strong_bullish | bullish | neutral | bearish | strong_bearish | insufficient"
    )
    score: int = Field(ge=-5, le=5)
    summary: str = ""
    indicators: StockTechnicalIndicators = Field(default_factory=StockTechnicalIndicators)
    signals: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    methodology: str = ""


class StockMasterOut(ConfiguredBaseModel):
    code: str
    name: str
    exchange: str | None = None


class StockMasterListResponse(ConfiguredBaseModel):
    items: list[StockMasterOut]
    total: int = 0


class StockIndustryOut(ConfiguredBaseModel):
    code: str
    name: str | None = None
    source: str = Field(description="em（东方财富）| cninfo（巨潮回退）")
    industry_name: str


class StockIndustryListResponse(ConfiguredBaseModel):
    items: list[StockIndustryOut]
    total: int = 0


class FundamentalsQueryResult(ConfiguredBaseModel):
    """通用单股票基本面查询响应（行结构随数据源，不强 schema）。"""

    code: str
    rows: list[dict[str, Any]] = Field(default_factory=list)
    total: int = 0


class QualityIssueOut(ConfiguredBaseModel):
    """单条数据质量问题。"""

    check: str
    dataset: str
    detail: str
    severity: str = Field(description="error | warning")


class DatasetQualityOut(ConfiguredBaseModel):
    """单数据集质量汇总。"""

    dataset: str
    row_count: int = 0
    ok: bool = True
    issues: list[QualityIssueOut] = Field(default_factory=list)


class ResearchQualityResponse(ConfiguredBaseModel):
    """研究仓库全部数据集的质量报告。"""

    generated_at: datetime
    ok: bool = True
    datasets: list[DatasetQualityOut] = Field(default_factory=list)
