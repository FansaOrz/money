"""ORM 模型汇总，便于 Base.metadata 收集全部表结构。"""

from app.models.account import Account
from app.models.candidate_pool import CandidatePool, CandidatePoolMember
from app.models.fund_catalog import FundCatalogEntry
from app.models.fund_holding import FundHolding
from app.models.fund_holdings_sync_status import FundHoldingsSyncStatus
from app.models.fund_industry import FundIndustryAllocation
from app.models.fund_nav import FundNav
from app.models.fund_profile import FundProfile
from app.models.fund_warehouse_sync_state import FundWarehouseSyncState
from app.models.import_ import Import, ImportStatus
from app.models.instrument import Instrument, InstrumentType
from app.models.market_index import IndexQuote, MarketIndex
from app.models.nav_sync_status import NavSyncStatus
from app.models.news import NewsItem
from app.models.news_analysis import FundNewsImpact, NewsEvent, NewsEventItem
from app.models.paper import (
    BacktestRun,
    PaperAccount,
    PaperHoldingDaily,
    PaperNavDaily,
    PaperPosition,
    PaperTrade,
    SignalSnapshot,
    StrategyVersion,
)
from app.models.performance_baseline import PerformanceBaseline
from app.models.portfolio_snapshot import PortfolioSnapshot
from app.models.position import Position
from app.models.quant_platform import (
    AuditLog,
    BrokerAccountLedger,
    BrokerFill,
    BrokerOrder,
    DataFieldProvenance,
    DataCorrection,
    DataQualityIssue,
    DataReadinessReport,
    PersistentJob,
    QuantImportRun,
    QuantDataRecord,
    RiskControlState,
    StrategyTransition,
)
from app.models.research import (
    IndexConstituent,
    IndexMembershipEvent,
    StockDailyBar,
    StockFinancialIndicator,
    StockIndustry,
    StockMaster,
    StockNameHistory,
    StockReportDisclosure,
    StockSyncState,
    StockUniverseSnapshot,
    StockValuation,
)
from app.models.sync_run import SyncRun
from app.models.stock_paper import (
    StockPaperAccount,
    StockPaperNavDaily,
    StockPaperPosition,
    StockPaperReceivable,
    StockPaperRun,
    StockPaperSignal,
    StockPaperTrade,
)
from app.models.transaction import Transaction, TransactionType

__all__ = [
    "Account",
    "BacktestRun",
    "AuditLog",
    "BrokerAccountLedger",
    "BrokerFill",
    "BrokerOrder",
    "CandidatePool",
    "CandidatePoolMember",
    "FundCatalogEntry",
    "FundHolding",
    "FundHoldingsSyncStatus",
    "FundIndustryAllocation",
    "FundNav",
    "FundNewsImpact",
    "FundProfile",
    "FundWarehouseSyncState",
    "DataFieldProvenance",
    "DataCorrection",
    "DataQualityIssue",
    "DataReadinessReport",
    "Import",
    "ImportStatus",
    "IndexConstituent",
    "IndexMembershipEvent",
    "IndexQuote",
    "Instrument",
    "InstrumentType",
    "MarketIndex",
    "NavSyncStatus",
    "NewsItem",
    "NewsEvent",
    "NewsEventItem",
    "PaperAccount",
    "PaperHoldingDaily",
    "PaperNavDaily",
    "PaperPosition",
    "PaperTrade",
    "PersistentJob",
    "PerformanceBaseline",
    "PortfolioSnapshot",
    "Position",
    "QuantDataRecord",
    "QuantImportRun",
    "RiskControlState",
    "SignalSnapshot",
    "StockDailyBar",
    "StockFinancialIndicator",
    "StockIndustry",
    "StockMaster",
    "StockNameHistory",
    "StockPaperAccount",
    "StockPaperNavDaily",
    "StockPaperPosition",
    "StockPaperReceivable",
    "StockPaperRun",
    "StockPaperSignal",
    "StockPaperTrade",
    "StockReportDisclosure",
    "StockSyncState",
    "StockUniverseSnapshot",
    "StockValuation",
    "StrategyVersion",
    "StrategyTransition",
    "SyncRun",
    "Transaction",
    "TransactionType",
]
