"""A股多因子研究 Schema：因子 / 信号 / 回测的请求响应模型。

仅包含只读研究能力，不涉及任何实盘下单；路由挂载 /api/stocks/research/*。
"""

from typing import Literal

from pydantic import Field, model_validator

from app.schemas.common import ConfiguredBaseModel


# ---------------------------------------------------------------------------
# 共用
# ---------------------------------------------------------------------------


class FactorWeightsConfig(ConfiguredBaseModel):
    """因子族权重覆盖（缺省与服务层常量一致：30/25/20/15/10）。"""

    quality: float = Field(default=0.30, ge=0.0, le=1.0, description="质量族权重")
    value: float = Field(default=0.25, ge=0.0, le=1.0, description="价值族权重")
    momentum: float = Field(default=0.20, ge=0.0, le=1.0, description="12-1 动量权重")
    trend: float = Field(default=0.15, ge=0.0, le=1.0, description="趋势权重")
    lowvol: float = Field(default=0.10, ge=0.0, le=1.0, description="低波动权重")


class CostModelConfig(ConfiguredBaseModel):
    """A股交易费用模型（小数口径）。"""

    commission_rate: float = Field(default=0.00025, ge=0.0, le=0.05, description="双边佣金率")
    min_commission: float = Field(default=5.0, ge=0.0, le=1000.0, description="单笔最低佣金（元）")
    stamp_tax_rate: float = Field(default=0.0005, ge=0.0, le=0.05, description="印花税率（仅卖出）")
    slippage_rate: float = Field(default=0.001, ge=0.0, le=0.05, description="双边滑点率")
    market_impact_coefficient: float = Field(
        default=0.002, ge=0.0, le=0.1, description="成交量参与率平方根冲击系数"
    )
    volatility_slippage_coefficient: float = Field(
        default=0.10, ge=0.0, le=1.0, description="近期日波动对滑点的系数"
    )
    max_total_slippage: float = Field(
        default=0.03, ge=0.0, le=0.2, description="单边总滑点上限"
    )


# ---------------------------------------------------------------------------
# POST /api/stocks/research/factors
# ---------------------------------------------------------------------------


class StockFactorsRequest(ConfiguredBaseModel):
    """因子横截面请求。"""

    as_of: str = Field(description="打分日 YYYY-MM-DD（仅用该日及之前的 PIT 数据）")
    candidate_codes: list[str] | None = Field(
        default=None, min_length=1, max_length=5000,
        description="候选股票池；缺省为全市场（经 universe 过滤）",
    )
    apply_universe_filter: bool = Field(
        default=True, description="是否先应用 ST/停牌/次新/流动性 universe 过滤"
    )
    min_avg_amount: float = Field(
        default=5e7, gt=0, description="流动性过滤：近20日日均成交额下限（元）"
    )
    weights: FactorWeightsConfig | None = Field(
        default=None, description="因子族权重覆盖；缺省 30/25/20/15/10"
    )


class StockFactorRow(ConfiguredBaseModel):
    """一只股票的因子明细（原始值、行业内 z、族分、复合分）。"""

    code: str
    name: str
    industry: str
    raw: dict[str, float | None] = Field(
        default_factory=dict, description="原始因子值（未经横截面处理）"
    )
    zscores: dict[str, float | None] = Field(
        default_factory=dict, description="行业内 winsorize 后 z-score（方向已调整）"
    )
    quality: float | None = None
    value: float | None = None
    momentum: float | None = None
    trend: float | None = None
    lowvol: float | None = None
    composite: float = Field(description="复合分（族权重加权和，缺失族归一化）")
    data_coverage: float = Field(description="可用因子族权重覆盖率")
    eligible: bool = Field(description="是否达到组合入选的数据门槛")
    rank: int = Field(description="复合分排名（1 为最高）")
    data_warnings: list[str] = Field(default_factory=list)


class StockFactorsResponse(ConfiguredBaseModel):
    """因子横截面响应。"""

    as_of: str
    universe_count: int = Field(description="通过 universe 过滤的股票数")
    excluded_count: int = Field(description="被 universe 过滤剔除的股票数")
    exclusion_reasons: dict[str, int] = Field(
        default_factory=dict, description="剔除原因 → 只数（首要原因口径）"
    )
    industry_count: int = Field(description="universe 覆盖的行业数")
    factor_weights: dict[str, float] = Field(default_factory=dict)
    factor_diagnostics: dict[str, object] = Field(default_factory=dict)
    rows: list[StockFactorRow] = Field(
        default_factory=list, description="按复合分降序（规模受控截断）"
    )
    truncated: bool = Field(default=False, description="行数是否被截断")
    methodology: str = ""
    warnings: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# POST /api/stocks/research/signals
# ---------------------------------------------------------------------------


class StockSignalsRequest(StockFactorsRequest):
    """当期信号请求：因子横截面 + 目标组合。"""

    top_n: int = Field(default=30, ge=1, le=200, description="入选股票数上限")
    max_stock_weight: float = Field(default=0.05, gt=0, le=1.0, description="单股权重上限")
    max_industry_weight: float = Field(default=0.20, gt=0, le=1.0, description="单行业权重上限")


class StockSignalItem(ConfiguredBaseModel):
    """一只入选股票的信号。"""

    code: str
    name: str
    industry: str
    composite: float
    rank: int
    weight: float = Field(description="目标权重（行业中性 + 单股/行业上限截断后）")
    quality: float | None = None
    value: float | None = None
    momentum: float | None = None
    trend: float | None = None
    lowvol: float | None = None
    reasons: list[str] = Field(default_factory=list, description="可解释理由")


class StockSignalsResponse(ConfiguredBaseModel):
    """当期信号响应。"""

    as_of: str
    trade_date: str | None = Field(
        default=None, description="预计成交日（T+1 交易日；未知交易日历时为 None）"
    )
    universe_count: int
    selected: list[StockSignalItem] = Field(default_factory=list)
    invested_weight: float = Field(description="股票仓位合计（其余为现金）")
    industry_weights: dict[str, float] = Field(
        default_factory=dict, description="行业权重分布（行业中性结果）"
    )
    methodology: str = ""
    warnings: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# POST /api/stocks/research/backtest
# ---------------------------------------------------------------------------


class StockBacktestRequest(ConfiguredBaseModel):
    """月调仓多因子回测请求。"""

    start_date: str = Field(description="回测起始日 YYYY-MM-DD")
    end_date: str = Field(description="回测截止日 YYYY-MM-DD")
    initial_capital: float = Field(default=1_000_000.0, gt=0, le=1e10)
    candidate_codes: list[str] | None = Field(
        default=None, min_length=1, max_length=5000,
        description="显式固定候选池；传入后不使用历史指数动态股票池",
    )
    universe_indices: list[str] = Field(
        default_factory=lambda: ["000300", "000905"],
        max_length=10,
        description="缺省按这些指数在每个信号日的历史成分并集回测",
    )
    initial_signal: bool = Field(
        default=False,
        description="是否在非月末区间起点主动建仓；缺省仅真实月末生成信号",
    )
    min_universe_data_coverage: float = Field(
        default=0.95,
        ge=0.5,
        le=1.0,
        description="动态历史股票池每个信号日的核心数据最低覆盖率",
    )
    max_volume_participation: float = Field(
        default=0.10,
        gt=0,
        le=0.50,
        description="单日模拟成交量不得超过当日成交量的比例",
    )
    minimum_trade_weight: float = Field(
        default=0.002,
        ge=0,
        le=0.05,
        description="目标权重变化小于该值时不交易",
    )
    minimum_holdings: int = Field(default=20, ge=5, le=200)
    max_annual_volatility: float = Field(
        default=0.25, gt=0, le=1.0, description="组合预期年化波动硬上限"
    )
    max_tracking_error: float = Field(
        default=0.15, gt=0, le=1.0, description="相对市值基准预期跟踪误差硬上限"
    )
    top_n: int = Field(default=30, ge=1, le=200)
    max_stock_weight: float = Field(default=0.05, gt=0, le=1.0)
    max_industry_weight: float = Field(default=0.20, gt=0, le=1.0)
    min_avg_amount: float = Field(default=5e7, gt=0)
    price_limit: float = Field(
        default=0.098, gt=0.0, le=0.30,
        description="涨跌停阈值（主板 10% 近似；触及即不可成交）",
    )
    benchmark_index: str | None = Field(
        default=None, description="指数基准代码（如 CSI300）；缺省或数据缺失时回退等权基准"
    )
    cost: CostModelConfig = Field(default_factory=CostModelConfig)

    @model_validator(mode="after")
    def _check_dates(self) -> "StockBacktestRequest":
        if self.start_date > self.end_date:
            raise ValueError("start_date 不能晚于 end_date")
        if self.candidate_codes:
            self.universe_indices = []
        return self


class StockCurvePoint(ConfiguredBaseModel):
    date: str
    equity: float = Field(description="组合总市值（含现金）")
    benchmark: float = Field(description="基准净值（起点 1.0）")


class StockTradeRecord(ConfiguredBaseModel):
    signal_date: str
    fill_date: str
    code: str
    action: Literal["buy", "sell"]
    price: float = Field(description="含滑点成交价")
    shares: float
    amount: float
    fee: float
    reason: str


class StockRebalanceDetail(ConfiguredBaseModel):
    signal_date: str = Field(description="调仓信号日（月内最后交易日）")
    target: dict[str, float] = Field(default_factory=dict, description="目标权重")
    cash_weight: float
    turnover: float = Field(description="本次换手率（Σ|Δw|/2）")
    blocked_codes: list[str] = Field(
        default_factory=list, description="因涨跌停/停牌被顺延的股票"
    )
    fills: list[StockTradeRecord] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    diagnostics: dict[str, object] = Field(default_factory=dict)
    order_events: list[dict[str, object]] = Field(
        default_factory=list, description="逐次下单、阻塞、部分成交、覆盖及过期轨迹"
    )


class StockBacktestSummary(ConfiguredBaseModel):
    total_return: float | None = None
    annual_return: float | None = None
    annual_volatility: float | None = None
    max_drawdown: float | None = None
    sharpe: float | None = None
    win_rate: float | None = None
    cvar95: float | None = None
    calmar: float | None = None


class StockValidationStats(ConfiguredBaseModel):
    """预测有效性统计（复合分 vs 下一期前瞻收益）。"""

    rank_ic_mean: float | None = None
    rank_ic_count: int = 0
    rank_ic_std: float | None = None
    rank_ic_ir: float | None = None
    rank_ic_t_stat: float | None = None
    rank_ic_hit_rate: float | None = None
    rank_ic_newey_west_se: float | None = None
    rank_ic_newey_west_t: float | None = None
    rank_ic_p_value: float | None = None
    rank_ic_95_ci: list[float] = Field(default_factory=list)
    rank_ic_bootstrap_95_ci: list[float] = Field(default_factory=list)
    ic_decay_autocorrelation: list[float | None] = Field(default_factory=list)
    mean_rank_turnover: float | None = None
    multiple_testing_warning: str | None = None
    quintile_period_count: int = 0
    quintile_returns: list[float | None] = Field(default_factory=list)
    quintile_spread: float | None = None
    quintile_kendall_tau: float | None = None
    quintile_monotonic: bool = False


class StockBacktestResult(ConfiguredBaseModel):
    """回测响应。"""

    params: dict = Field(default_factory=dict)
    attribution: dict[str, float] = Field(default_factory=dict)
    start_date: str
    end_date: str
    initial_capital: float
    final_value: float
    strategy: StockBacktestSummary
    benchmark: StockBacktestSummary
    benchmark_kind: str = Field(description="equal_weight 或 index:<code>")
    excess_return: float | None = None
    information_ratio: float | None = None
    total_fees: float
    avg_turnover: float
    rebalance_count: int
    validation: StockValidationStats
    curve: list[StockCurvePoint] = Field(default_factory=list)
    rebalances: list[StockRebalanceDetail] = Field(default_factory=list)
    trades: list[StockTradeRecord] = Field(
        default_factory=list, description="全部成交记录（按期聚合，规模受控）"
    )
    methodology: str = ""
    warnings: list[str] = Field(default_factory=list)


class StockWalkForwardRequest(StockBacktestRequest):
    """股票策略训练/验证/完全留出评估。"""

    top_n_grid: list[int] = Field(default_factory=lambda: [20, 30, 40])
    max_stock_weight_grid: list[float] = Field(
        default_factory=lambda: [0.03, 0.05]
    )
    embargo_days: int = Field(default=21, ge=0, le=126)


class StockWalkForwardResult(ConfiguredBaseModel):
    result: dict[str, object]


__all__ = [
    "CostModelConfig",
    "FactorWeightsConfig",
    "StockBacktestRequest",
    "StockBacktestResult",
    "StockBacktestSummary",
    "StockCurvePoint",
    "StockFactorRow",
    "StockFactorsRequest",
    "StockFactorsResponse",
    "StockRebalanceDetail",
    "StockSignalItem",
    "StockSignalsRequest",
    "StockSignalsResponse",
    "StockTradeRecord",
    "StockValidationStats",
    "StockWalkForwardRequest",
    "StockWalkForwardResult",
]
