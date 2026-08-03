"""基金发现量化模块 Schema。

仅包含只读研究能力，不涉及任何实盘下单。与既有 discovery 实现解耦：
本文件独立定义全部模型，路由挂载在 /api/discovery/quant/* 子路径下，
不修改/覆盖任何已有 discovery 文件（避免与进行中的候选池工作冲突）。
"""

from typing import Literal

from pydantic import Field

from app.schemas.common import ConfiguredBaseModel
from app.schemas.quant import ValidationCostModel, WalkForwardWindow
from app.schemas.quant_v2 import FeeModelConfig


# ---------------------------------------------------------------------------
# 因子榜
# ---------------------------------------------------------------------------

FactorSortField = Literal[
    "return_1m",
    "return_3m",
    "return_1y",
    "return_3y",
    "annual_volatility",
    "max_drawdown",
    "sharpe",
    "sortino",
    "calmar",
    "cvar95",
    "momentum_12_1",
    "quantile",
]


class FactorBoardQuery(ConfiguredBaseModel):
    """因子榜查询参数（服务层内部使用；路由层由 Query 参数构造）。

    sort 缺省按 12-1 动量降序；window 为风险/比率类指标的回溯交易日数。
    """

    codes: list[str] | None = None
    sort: FactorSortField = "momentum_12_1"
    order: Literal["asc", "desc"] = "desc"
    window: int = Field(default=252, ge=20, le=756)
    limit: int = Field(default=20, ge=1, le=100)
    offset: int = Field(default=0, ge=0)
    min_samples: int = Field(default=60, ge=2, le=500)


class FactorBoardItem(ConfiguredBaseModel):
    """因子榜中一只基金的横截面指标快照（仅研究展示，不构成投资建议）。

    收益为区间收益（小数）；波动/夏普/索提诺为年化口径；CVaR95 为最差
    5% 日收益均值；Calmar 为年化收益 / |最大回撤|；quantile 为同类
    （同市场层）内综合动量分位数 ∈[0,1]。
    """

    rank: int = Field(description="按排序因子在有效候选中的名次（1 起，并列顺延）")
    code: str
    name: str
    market: str = Field(description="市场层分类，如 cn / hk / us_spx / gold / bond")
    market_label: str = Field(description="市场层中文标签")
    family: str = Field(description="基金家族（同家族 A/C/D 份额归并键）")
    sample_count: int = Field(description="参与计算的净值样本数")

    return_1m: float | None = Field(default=None, description="近 21 个交易日收益（小数）")
    return_3m: float | None = Field(default=None, description="近 63 个交易日收益（小数）")
    return_1y: float | None = Field(default=None, description="近 252 个交易日收益（小数）")
    return_3y: float | None = Field(default=None, description="近 756 个交易日收益（小数）")
    annual_volatility: float | None = Field(default=None, description="年化波动率（小数）")
    max_drawdown: float | None = Field(default=None, description="窗口内最大回撤（负数小数）")
    sharpe: float | None = Field(default=None, description="夏普比率（年化，无风险利率 2%）")
    sortino: float | None = Field(default=None, description="索提诺比率（年化，下行偏差口径）")
    calmar: float | None = Field(default=None, description="Calmar：年化收益 / |最大回撤|")
    cvar95: float | None = Field(default=None, description="CVaR95：最差 5% 日收益均值（小数）")
    momentum_12_1: float | None = Field(
        default=None, description="绝对动量 12-1（t-21 对 t-252 区间收益，小数）"
    )
    quantile: float | None = Field(
        default=None, description="同类（同市场层）内动量分位数 ∈[0,1]，最高为 (n-1)/n"
    )


class FactorBoardResponse(ConfiguredBaseModel):
    """因子榜（响应，分页）。"""

    as_of: str | None = Field(default=None, description="因子基准日（最新共同净值日）")
    methodology: str = Field(default="", description="因子口径说明")
    total: int = Field(description="有效候选总数（分页前）")
    limit: int
    offset: int
    sort: str
    order: str
    window: int
    pool_size: int = Field(description="候选池装载成功的基金数")
    excluded_count: int = Field(default=0, description="因样本不足被剔除的候选数")
    items: list[FactorBoardItem] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# 双动量
# ---------------------------------------------------------------------------


class DualMomentumQuery(ConfiguredBaseModel):
    """双动量（Dual Momentum）查询参数（服务层内部使用）。

    相对动量：候选内按 12-1 动量取前 top_n 只等权；
    绝对动量：动量 ≤ 0 的候选不计入，若 top_n 全部 ≤ 0 则整体回避
    （hold_offense=false），与 Gary Antonacci 的双动量口径一致。
    """

    codes: list[str] | None = None
    top_n: int = Field(default=1, ge=1, le=10)


class DualMomentumItem(ConfiguredBaseModel):
    """双动量候选明细（按 12-1 动量降序）。"""

    rank: int
    code: str
    name: str
    market: str
    market_label: str
    momentum_12_1: float | None = Field(default=None, description="绝对动量 12-1（小数）")
    selected: bool = Field(description="是否入选当期进攻组合（相对动量前 top_n 且动量 > 0）")
    weight: float = Field(description="目标权重（小数）；未入选为 0")


class DualMomentumResponse(ConfiguredBaseModel):
    """双动量信号（响应）。hold_offense=false 时全部候选权重为 0（回避）。"""

    as_of: str | None = Field(default=None, description="信号基准日（最新净值日）")
    methodology: str = Field(default="", description="策略方法说明")
    top_n: int
    candidate_count: int
    hold_offense: bool = Field(
        description="是否持有进攻组合：任一前 top_n 候选动量 > 0 为 true，否则整体回避"
    )
    cash_weight: float = Field(description="现金权重（回避时为 1）")
    items: list[DualMomentumItem] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# 发现页 V2 回测 / 验证请求（候选池由 pool_id 或显式 codes 提供）
# ---------------------------------------------------------------------------


class DiscoveryBacktestV2Request(ConfiguredBaseModel):
    """基金发现 V2 回测请求：候选池 pool_id 与显式 codes 二选一（codes 优先）。

    其余参数与 /api/quant/v2/backtest 一致（详见该接口的 methodology）。
    """

    pool_id: int | None = Field(default=None, description="候选池 ID（需候选池模型可用）")
    codes: list[str] | None = Field(
        default=None, min_length=1, max_length=250,
        description="显式候选基金代码；提供时优先于 pool_id",
    )
    start_date: str | None = Field(default=None, description="回测起始日 YYYY-MM-DD")
    end_date: str | None = Field(default=None, description="回测截止日 YYYY-MM-DD")
    initial_capital: float = Field(default=10000.0, gt=0, le=100_000_000)
    top_n: int = Field(default=8, ge=1, le=30)
    rebalance_interval_months: int = Field(default=1, ge=1, le=12)
    target_vol: float | None = Field(default=None, gt=0.01, le=0.50)
    max_fund_weight: float | None = Field(default=None, gt=0.0, le=1.0)
    max_family_weight: float | None = Field(default=None, gt=0.0, le=1.0)
    max_qdii_weight: float | None = Field(default=None, gt=0.0, le=1.0)
    fee_model: FeeModelConfig = Field(default_factory=FeeModelConfig)


class DiscoveryValidationRequest(ConfiguredBaseModel):
    """基金发现量化验证请求：候选池 pool_id 与显式 codes 二选一（codes 优先）。

    其余参数与 /api/quant/validation 一致（详见该接口的 methodology）。
    """

    pool_id: int | None = Field(default=None, description="候选池 ID（需候选池模型可用）")
    codes: list[str] | None = Field(
        default=None, min_length=2, max_length=250,
        description="显式候选基金代码；提供时优先于 pool_id",
    )
    as_of: str | None = Field(default=None, description="快照基准日 YYYY-MM-DD；缺省使用全部历史")
    window: WalkForwardWindow = Field(default_factory=WalkForwardWindow)
    top_n: int = Field(default=3, ge=1, le=20)
    rebalance_interval: int = Field(default=1, ge=1, le=60)
    include_costs: bool = Field(default=True)
    cost_model: ValidationCostModel = Field(default_factory=ValidationCostModel)
    trial_count: int = Field(default=1, ge=1, le=10000)
    bootstrap_resamples: int = Field(default=500, ge=100, le=5000)
    block_length: int | None = Field(default=None, ge=1, le=250)
    seed: int = Field(default=42)
