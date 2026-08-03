"""主要市场指数相关 Schema。"""

from datetime import date, datetime

from pydantic import Field

from app.schemas.common import ConfiguredBaseModel, DecimalStr


class IndexSummary(ConfiguredBaseModel):
    """单个指数的最新行情摘要（响应）。"""

    code: str = Field(description="内部统一代码，例如 SH000001 / SPX")
    name: str
    name_en: str | None = None
    market: str = Field(description="cn | hk | us")
    currency: str
    latest_date: date | None = Field(default=None, description="最新交易日；无数据时为 null")
    close: DecimalStr | None = None
    change_pct: DecimalStr | None = Field(default=None, description="最新交易日涨跌幅（%）")
    volume: int | None = None


class IndexListResponse(ConfiguredBaseModel):
    """指数摘要列表（响应）。"""

    items: list[IndexSummary]
    total: int


class IndexQuoteOut(ConfiguredBaseModel):
    """单日日线行情（响应）。"""

    date: date
    open: DecimalStr | None = None
    high: DecimalStr | None = None
    low: DecimalStr | None = None
    close: DecimalStr
    volume: int | None = None
    change_pct: DecimalStr | None = None


class IndexHistoryResponse(ConfiguredBaseModel):
    """指数历史日线（响应）。"""

    code: str
    name: str
    days: int
    items: list[IndexQuoteOut]


class IndexSyncResult(ConfiguredBaseModel):
    """手动/调度同步指数行情的结果。"""

    synced_at: datetime
    total_indices: int = 0
    updated_indices: int = 0
    failed: int = 0
    rows: int = Field(default=0, description="本次 upsert 的行情行数")
    errors: list[str] = Field(default_factory=list)
