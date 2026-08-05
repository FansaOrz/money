"""A 股规则策略前向模拟 API schema。"""

from datetime import date

from pydantic import Field

from app.schemas.common import ConfiguredBaseModel


class StockPaperStrategyInfo(ConfiguredBaseModel):
    version_id: int
    name: str
    status: str
    trial_start: str
    trial_end: str
    calendar_days_elapsed: int
    calendar_days_remaining: int
    observation_progress: float
    candidate_count: int
    validation_scope: str
    investment_approval_eligible: bool
    mandate_version: str
    mandate_sha256: str
    result_interpretation: str
    approval_blocker: str | None = None
    params: dict = Field(default_factory=dict)


class StockPaperPositionOut(ConfiguredBaseModel):
    code: str
    name: str
    industry: str
    shares: float
    cost: float
    price: float | None = None
    market_value: float | None = None
    weight: float | None = None
    pnl: float | None = None


class StockPaperTradeOut(ConfiguredBaseModel):
    id: int
    trade_date: str
    signal_date: str
    code: str
    name: str
    side: str
    shares: float
    price: float
    amount: float
    fee: float
    target_weight: float
    reason: str


class StockPaperSignalOut(ConfiguredBaseModel):
    id: int
    signal_date: str
    execute_on: str | None = None
    status: str
    universe_count: int
    selected_count: int
    invested_weight: float
    items: list[dict] = Field(default_factory=list)
    order_state: dict[str, dict] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)


class StockPaperCancelRequest(ConfiguredBaseModel):
    reason: str = Field(min_length=1, max_length=300)


class StockPaperHistoryPoint(ConfiguredBaseModel):
    date: str
    nav: float
    benchmark_nav: float
    total_value: float
    available_cash: float = 0.0
    frozen_cash: float = 0.0
    receivable_cash: float = 0.0
    settled_cash: float = 0.0
    cash_interest: float = 0.0
    cash_conservation_error: float = 0.0
    daily_return: float | None = None
    benchmark_daily_return: float | None = None
    rebalanced: bool = False


class StockPaperMetrics(ConfiguredBaseModel):
    total_return: float | None = None
    benchmark_return: float | None = None
    excess_return: float | None = None
    annual_return: float | None = None
    annual_volatility: float | None = None
    max_drawdown: float | None = None
    sharpe: float | None = None
    win_rate: float | None = None
    information_ratio: float | None = None
    trading_days: int = 0
    rebalance_count: int = 0
    trade_count: int = 0
    total_fees: float = 0.0


class StockPaperReadiness(ConfiguredBaseModel):
    ready: bool
    status: str
    universe_count: int
    daily_ready_count: int
    industry_ready_count: int
    financial_ready_count: int
    valuation_ready_count: int
    latest_data_date: str | None = None
    source_health: dict[str, dict] = Field(default_factory=dict)
    blockers: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class StockPaperSummary(ConfiguredBaseModel):
    started: bool
    account_id: int | None = None
    account_name: str | None = None
    as_of: str | None = None
    initial_capital: float = 1_000_000.0
    cash: float = 1_000_000.0
    frozen_cash: float = 0.0
    receivable_cash: float = 0.0
    settled_cash: float = 1_000_000.0
    cash_interest: float = 0.0
    cash_ledger: dict = Field(default_factory=dict)
    market_value: float = 0.0
    total_value: float = 1_000_000.0
    nav: float = 1.0
    benchmark_nav: float = 1.0
    strategy: StockPaperStrategyInfo | None = None
    readiness: StockPaperReadiness
    metrics: StockPaperMetrics = Field(default_factory=StockPaperMetrics)
    positions: list[StockPaperPositionOut] = Field(default_factory=list)
    latest_signal: StockPaperSignalOut | None = None
    history: list[StockPaperHistoryPoint] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class StockPaperRunResponse(ConfiguredBaseModel):
    account_id: int
    run_date: str
    skipped: bool
    status: str
    signal_generated: bool
    rebalanced: bool
    trade_count: int
    total_value: float
    nav: float
    benchmark_nav: float
    warnings: list[str] = Field(default_factory=list)


class StockPaperPrepareRequest(ConfiguredBaseModel):
    start_date: date
    end_date: date
    top_n_grid: list[int] = Field(default_factory=lambda: [30])
    max_stock_weight_grid: list[float] = Field(default_factory=lambda: [0.05])
    embargo_days: int = Field(default=21, ge=5, le=63)
    create_new_version: bool = False


class StockPaperPrepareResponse(ConfiguredBaseModel):
    version_id: int
    status: str
    account_id: int
    data_date: date
    validation: dict[str, object] = Field(default_factory=dict)
