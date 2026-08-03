"""PDF 导入相关 Schema。"""

from app.schemas.common import ConfiguredBaseModel, DecimalStr
from app.schemas.portfolio import PortfolioSummary


class PreviewPosition(ConfiguredBaseModel):
    fund_code: str
    fund_name: str
    shares: DecimalStr
    nav: DecimalStr | None
    nav_date: str | None
    market_value: DecimalStr | None
    cost_price: DecimalStr | None = None
    profit: DecimalStr | None = None
    return_rate: DecimalStr | None = None
    profit_available: bool = False
    cost_coverage_rate: DecimalStr | None = None


class FundNavHistoryItem(ConfiguredBaseModel):
    nav_date: str
    unit_nav: DecimalStr
    accumulated_nav: DecimalStr | None
    daily_growth_rate: DecimalStr | None


class FundTradePoint(ConfiguredBaseModel):
    trade_date: str
    type: str
    amount: DecimalStr
    shares: DecimalStr | None


class FundNavHistoryResponse(ConfiguredBaseModel):
    fund_code: str
    fund_name: str
    items: list[FundNavHistoryItem]
    trades: list[FundTradePoint]
    total: int


class PreviewTransaction(ConfiguredBaseModel):
    transaction_date: str
    confirmation_date: str | None
    fund_code: str
    fund_name: str
    transaction_type: str
    amount: DecimalStr
    shares: DecimalStr | None
    nav: DecimalStr | None
    fee: DecimalStr
    status: str = "已确认"


class ImportPreviewResponse(ConfiguredBaseModel):
    import_id: int
    file_name: str
    document_type: str
    snapshot_date: str | None
    summary: PortfolioSummary | None
    positions: list[PreviewPosition]
    transactions: list[PreviewTransaction]
    warnings: list[str]
    status: str
    message: str | None


class CommitResultResponse(ConfiguredBaseModel):
    ok: bool
    message: str
    positions_written: int = 0
    transactions_written: int = 0


class ImportItem(ConfiguredBaseModel):
    id: int
    file_name: str
    document_type: str | None
    status: str
    record_count: int
    message: str | None
    created_at: str


class ImportListResponse(ConfiguredBaseModel):
    items: list[ImportItem]
    total: int
