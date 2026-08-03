"""稳健组合策略 V2 Schema。

仅包含只读研究/回测能力，不涉及任何实盘下单。
与 v1（schemas/quant.py）相互独立：V2 的模型全部定义在本文件，
路由挂载在 /api/quant/v2/* 子路径下。
"""

from typing import Literal

from pydantic import Field

from app.schemas.common import ConfiguredBaseModel


# ---------------------------------------------------------------------------
# 费用模型（接口预留：默认零费用，未来可扩展申购/赎回/滑点分档）
# ---------------------------------------------------------------------------


class FeeModelConfig(ConfiguredBaseModel):
    """费用模型配置（接口预留）。

    默认全部为零（与 v1 回测口径一致）；后续接入真实费率时仅需
    扩展本模型并在服务层实现 apply_fee 钩子，回测主流程不变。
    """

    buy_fee_rate: float = Field(default=0.0, ge=0.0, le=0.10, description="买入（申购）费率，小数")
    sell_fee_rate: float = Field(default=0.0, ge=0.0, le=0.10, description="卖出（赎回）费率，小数")
    slippage_rate: float = Field(default=0.0, ge=0.0, le=0.10, description="滑点率，小数")
    min_fee: float = Field(default=0.0, ge=0.0, description="单笔最低费用（金额）")


# ---------------------------------------------------------------------------
# 回测请求 / 响应
# ---------------------------------------------------------------------------


class BacktestV2Request(ConfiguredBaseModel):
    """稳健组合 V2 回测请求。

    - candidate_codes 为候选基金池；缺省时使用当前持仓基金；
    - 月频调仓：每月最后一个交易日打分，T+1 按信号确认日净值成交
      （QDII 基金 T+2，详见服务层 methodology）；
    - rebalance_interval_months > 1 时每隔若干个月调仓一次，其余月份持有不动。
    """

    candidate_codes: list[str] | None = Field(
        default=None,
        min_length=1,
        max_length=1000,
        description="候选基金代码池；缺省时使用当前持仓基金",
    )
    start_date: str | None = Field(default=None, description="回测起始日 YYYY-MM-DD，缺省为最早净值")
    end_date: str | None = Field(default=None, description="回测截止日 YYYY-MM-DD，缺省为最新净值")
    initial_capital: float = Field(default=10000.0, gt=0, le=100_000_000, description="初始资金")

    top_n: int = Field(default=8, ge=1, le=30, description="每期入选基金数上限")
    rebalance_interval_months: int = Field(
        default=1, ge=1, le=12, description="调仓间隔（月）：1 为每月调仓"
    )

    # ---- 覆盖默认风险参数（缺省使用服务层常量）----
    target_vol: float | None = Field(
        default=None, gt=0.01, le=0.50, description="组合年化波动目标（默认 0.10）"
    )
    max_fund_weight: float | None = Field(
        default=None, gt=0.0, le=1.0, description="单基金权重上限（默认 0.08）"
    )
    max_family_weight: float | None = Field(
        default=None, gt=0.0, le=1.0, description="同基金家族合计权重上限（默认 0.10）"
    )
    max_qdii_weight: float | None = Field(
        default=None, gt=0.0, le=1.0, description="QDII（海外）合计权重上限（默认 0.30）"
    )

    fee_model: FeeModelConfig = Field(
        default_factory=FeeModelConfig, description="费用模型（默认零费用，接口预留）"
    )


class TradeV2(ConfiguredBaseModel):
    """回测过程中的一笔成交记录（仅研究展示，不会真实下单）。"""

    signal_date: str = Field(description="信号日（调仓打分基准日）")
    fill_date: str = Field(description="成交日（T+1；QDII 为 T+2）")
    code: str
    name: str
    action: Literal["buy", "sell"]
    weight_change: float = Field(description="权重变化（正数小数）")
    amount: float = Field(description="成交金额（正数）")
    fee: float = Field(description="费用（按费用模型计算，默认 0）")
    price: float = Field(description="成交净值")
    settle_lag: int = Field(description="成交滞后：1=T+1，2=T+2（QDII）")
    reason: str = Field(description="调仓原因说明")


class BacktestV2CurvePoint(ConfiguredBaseModel):
    """净值曲线上的一个点（策略与基准同日期对齐）。"""

    date: str
    strategy: float = Field(description="策略净值（初始为 1）")
    benchmark: float = Field(description="基准净值（候选池等权买入持有，初始为 1）")


class RebalanceV2Detail(ConfiguredBaseModel):
    """一次月频调仓的明细。"""

    index: int = Field(description="调仓序号，从 1 开始")
    signal_date: str = Field(description="信号日（每月最后一个交易日）")
    fill_date: str = Field(description="成交日（T+1；QDII 为 T+2）")
    holdings: dict[str, float] = Field(
        default_factory=dict, description="本期目标权重 {基金代码: 权重}，不卖空且合计 ≤ 1"
    )
    cash_weight: float = Field(description="现金权重（含波动率目标降仓与约束截断）")
    turnover: float = Field(description="本次换手率（Σ|目标-漂移|/2，小数）")
    frozen: bool = Field(description="是否触发冻结（高波动+急反弹，沿用上一期持仓）")
    allocation_method: str = Field(
        description="层内配置方法：hrp / inverse_vol / equal_weight"
    )
    realized_vol: float | None = Field(
        default=None, description="组合 EWMA60 年化波动（信号日前，小数）"
    )
    vol_scalar: float = Field(description="波动率目标仓位系数 ∈(0,1]（1 为满仓）")
    reason: str = Field(default="", description="本期调仓的可解释说明")


class BacktestV2Summary(ConfiguredBaseModel):
    """一组净值序列（策略或基准）的汇总指标。"""

    total_return: float | None = Field(default=None, description="区间总收益率（小数）")
    annual_return: float | None = Field(default=None, description="年化收益率（按 252 交易日折算）")
    annual_volatility: float | None = Field(default=None, description="年化波动率（小数）")
    max_drawdown: float | None = Field(default=None, description="最大回撤（负数小数）")
    sharpe: float | None = Field(default=None, description="夏普比率（年化，无风险利率 2%）")
    win_rate: float | None = Field(default=None, description="日收益胜率（小数）")


class BacktestV2Result(ConfiguredBaseModel):
    """稳健组合 V2 回测结果（响应）。"""

    params: dict = Field(default_factory=dict, description="实际生效的回测参数")
    start_date: str = Field(description="首个可交易日期（净值曲线起点）")
    end_date: str = Field(description="最后净值日期")
    initial_capital: float

    strategy: BacktestV2Summary
    benchmark: BacktestV2Summary
    excess_return: float | None = Field(
        default=None, description="超额收益：策略总收益 - 基准总收益（小数）"
    )
    avg_turnover: float = Field(default=0.0, description="平均每次调仓换手率（小数）")
    rebalance_count: int = Field(description="实际调仓次数（不含冻结期）")
    frozen_count: int = Field(description="冻结期数（高波动+急反弹沿用持仓）")
    total_fees: float = Field(description="累计费用（按费用模型，默认 0）")

    curve: list[BacktestV2CurvePoint] = Field(
        default_factory=list, description="策略/基准净值曲线（均匀抽样，规模受控）"
    )
    rebalances: list[RebalanceV2Detail] = Field(default_factory=list, description="调仓明细")
    trades: list[TradeV2] = Field(default_factory=list, description="成交记录（规模受控）")

    methodology: str = Field(default="", description="策略方法说明")
    warnings: list[str] = Field(default_factory=list, description="数据或参数层面的提示")


# ---------------------------------------------------------------------------
# 当前信号（GET /api/quant/v2/signals）
# ---------------------------------------------------------------------------


class SignalV2Item(ConfiguredBaseModel):
    """一只入选基金的当期目标信号（仅研究展示，不构成投资建议）。"""

    code: str
    name: str
    market: str = Field(description="市场层分类，如 cn / hk / us / gold / bond / money / overseas")
    family: str = Field(description="基金家族（同家族 A/C/D 份额已去重）")
    momentum_12_1: float = Field(description="绝对动量 12-1（t-21 对 t-252 区间收益，小数）")
    rank_in_market: int = Field(description="同类市场层内动量排名（1 为最强）")
    market_candidates: int = Field(description="该市场层内通过绝对动量过滤的候选数")
    weight: float = Field(description="目标权重（小数，已含全部约束与波动率目标）")
    reasons: list[str] = Field(default_factory=list, description="可解释理由")


class SignalsV2Response(ConfiguredBaseModel):
    """稳健组合 V2 当期信号（响应）。"""

    as_of: str | None = Field(default=None, description="信号基准日（最新净值日期）YYYY-MM-DD")
    trade_date: str | None = Field(
        default=None, description="预计成交日（基准日 T+1；QDII 为 T+2）"
    )
    methodology: str = Field(default="", description="策略方法说明")
    candidate_count: int = Field(description="装载成功的候选基金数")
    eligible_count: int = Field(description="通过绝对动量>0 过滤的候选数")
    selected: list[SignalV2Item] = Field(default_factory=list, description="入选基金信号")
    cash_weight: float = Field(description="现金权重（小数）")
    realized_vol: float | None = Field(
        default=None, description="组合 EWMA60 年化波动（小数）"
    )
    vol_scalar: float = Field(description="波动率目标仓位系数 ∈(0,1]")
    frozen: bool = Field(description="当前是否处于冻结状态（高波动+急反弹）")
    freeze_reason: str | None = Field(default=None, description="冻结原因说明")
    warnings: list[str] = Field(default_factory=list)
