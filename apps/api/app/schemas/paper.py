"""模拟交易（Paper Trading）Schema。

全部为模拟交易能力，不涉及任何真实下单。金额相关沿用 DecimalStr，
权重/收益率用小数 float（研究场景精度足够）。
"""

from typing import Any, Literal

from pydantic import Field

from app.schemas.common import ConfiguredBaseModel, DecimalStr


# ---------------------------------------------------------------------------
# 账户摘要
# ---------------------------------------------------------------------------


class PaperStrategyInfo(ConfiguredBaseModel):
    """账户绑定的策略版本配置。"""

    version_id: int
    name: str
    initial_capital: DecimalStr
    rebalance_interval: int = Field(description="调仓间隔（交易日）")
    fee_rate: float = Field(description="双边简化费用率（小数，买卖各收一次）")
    top_n: int = Field(description="目标组合最大基金数")


class PaperSummary(ConfiguredBaseModel):
    """模拟账户摘要（响应）。"""

    account_id: int
    account_name: str
    strategy: PaperStrategyInfo
    currency: str
    cash: DecimalStr
    market_value: DecimalStr
    total_value: DecimalStr
    # 单位净值（total_value / initial_capital，起点 1.0）；无任何净值记录时为 None
    nav: float | None = Field(default=None, description="最新单位净值（起点 1.0）")
    cumulative_return: float | None = Field(default=None, description="累计收益率（小数）")
    total_return: float | None = Field(default=None, description="区间总收益率（同累计收益率）")
    annual_return: float | None = Field(default=None, description="年化收益率（按 252 交易日折算）")
    max_drawdown: float | None = Field(default=None, description="最大回撤（负数小数）")
    sharpe: float | None = Field(default=None, description="夏普比率（年化，无风险利率 2%）")
    win_rate: float | None = Field(default=None, description="日收益胜率（小数）")
    benchmark_nav: float | None = Field(default=None, description="候选池等权基准最新净值（起点 1.0）")
    benchmark_return: float | None = Field(default=None, description="基准累计收益率（小数）")
    excess_return: float | None = Field(default=None, description="相对基准超额收益（小数）")
    position_count: int = Field(description="当前持仓基金数")
    trade_count: int = Field(description="累计虚拟成交笔数")
    rebalance_count: int = Field(description="累计调仓次数")
    total_fees: DecimalStr = Field(description="累计费用（双边 0.1%）")
    start_date: str | None = Field(default=None, description="首个净值日期 YYYY-MM-DD")
    last_run_date: str | None = Field(default=None, description="最近一次运行日期 YYYY-MM-DD")
    next_rebalance_in: int | None = Field(
        default=None, description="距下次调仓的交易日数；尚无运行记录时为 None"
    )
    warnings: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# 历史净值
# ---------------------------------------------------------------------------


class PaperNavPoint(ConfiguredBaseModel):
    """每日净值记录（策略与基准）。"""

    date: str
    cash: DecimalStr
    market_value: DecimalStr
    total_value: DecimalStr
    nav: float = Field(description="单位净值（起点 1.0）")
    daily_return: float | None = None
    cumulative_return: float | None = None
    benchmark_nav: float | None = None
    benchmark_daily_return: float | None = None
    fee_total: DecimalStr = Field(description="当日调仓费用合计")
    rebalanced: bool = Field(description="当日是否发生调仓")


class PaperHistoryResponse(ConfiguredBaseModel):
    """净值历史（响应）。"""

    account_id: int
    start_date: str | None = None
    end_date: str | None = None
    count: int
    items: list[PaperNavPoint] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# 当前持仓 / 每日持仓
# ---------------------------------------------------------------------------


class PaperPositionItem(ConfiguredBaseModel):
    """一只当前持仓（按最新估值日口径）。"""

    code: str
    name: str
    shares: DecimalStr
    cost: DecimalStr = Field(description="持仓成本（含买入费用）")
    nav: float | None = Field(default=None, description="最新净值")
    nav_date: str | None = Field(default=None, description="最新净值日期")
    market_value: DecimalStr | None = Field(default=None, description="最新市值")
    weight: float | None = Field(default=None, description="占组合总市值比例（小数）")
    profit: DecimalStr | None = Field(default=None, description="浮动盈亏（市值-成本）")
    profit_pct: float | None = Field(default=None, description="浮动盈亏率（小数）")


class PaperPositionsResponse(ConfiguredBaseModel):
    """当前持仓（响应）。"""

    account_id: int
    as_of: str | None = Field(default=None, description="最新估值日")
    cash: DecimalStr
    total_value: DecimalStr
    count: int
    items: list[PaperPositionItem] = Field(default_factory=list)


class PaperHoldingDayItem(ConfiguredBaseModel):
    """某日一只持仓的估值快照。"""

    date: str
    code: str
    name: str
    shares: DecimalStr
    nav: float
    market_value: DecimalStr
    weight: float


# ---------------------------------------------------------------------------
# 成交记录
# ---------------------------------------------------------------------------


class PaperTradeItem(ConfiguredBaseModel):
    """一笔虚拟成交。"""

    id: int
    date: str
    code: str
    name: str
    side: Literal["buy", "sell"]
    shares: DecimalStr
    price: DecimalStr
    amount: DecimalStr
    fee: DecimalStr
    target_weight: float | None = None


class PaperTradesResponse(ConfiguredBaseModel):
    """成交记录（响应，按日期倒序）。"""

    account_id: int
    count: int
    items: list[PaperTradeItem] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# 信号快照
# ---------------------------------------------------------------------------


class PaperSignalSnapshotItem(ConfiguredBaseModel):
    """一次调仓日固化的全候选信号快照。"""

    id: int
    signal_date: str
    as_of: str | None = None
    methodology: str
    candidate_count: int
    excluded_count: int
    observe_count: int
    selected_count: int
    items: list[dict[str, Any]] = Field(default_factory=list, description="screener 全候选信号")
    warnings: list[str] = Field(default_factory=list)


class PaperSignalsResponse(ConfiguredBaseModel):
    """信号快照列表（响应，按日期倒序）。"""

    account_id: int
    count: int
    items: list[PaperSignalSnapshotItem] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# 手动触发运行
# ---------------------------------------------------------------------------


class PaperRunRequest(ConfiguredBaseModel):
    """手动触发 run_paper_cycle 的请求。"""

    run_date: str | None = Field(
        default=None,
        description="运行基准日 YYYY-MM-DD，缺省为当日；净值数据晚于该日的记录不参与本次估值",
    )


class PaperRunResponse(ConfiguredBaseModel):
    """run_paper_cycle 执行结果。"""

    account_id: int
    run_date: str
    trading_day_index: int = Field(description="自账户建立以来经过的净值交易日数量")
    rebalanced: bool
    trade_count: int = Field(description="本次虚拟成交笔数")
    fee_total: DecimalStr = Field(description="本次调仓费用合计")
    total_value: DecimalStr
    nav: float
    daily_return: float | None = None
    cumulative_return: float | None = None
    benchmark_nav: float | None = None
    skipped: bool = Field(description="是否命中幂等（同日已运行，直接返回已有结果）")
    warnings: list[str] = Field(default_factory=list)
