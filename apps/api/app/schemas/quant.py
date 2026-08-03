"""量化研究模块 Schema。

仅包含只读研究/回测能力，不涉及任何实盘下单。
数值以 float 为主（研究场景精度足够），金额相关沿用 DecimalStr。
"""

from typing import Literal

from pydantic import Field, model_validator

from app.schemas.common import ConfiguredBaseModel, DecimalStr


# ---------------------------------------------------------------------------
# 单基金指标
# ---------------------------------------------------------------------------


class FundIndicators(ConfiguredBaseModel):
    """单基金量化指标（响应）。

    基于 FundNav 历史净值（优先累计净值，缺失时回退单位净值）计算。
    样本不足时相应字段为 None；无净值数据时 data_available=false，指标字段为空。
    """

    code: str
    name: str
    start_date: str
    end_date: str
    sample_count: int = Field(description="参与计算的净值样本数")
    data_available: bool = Field(
        default=True, description="是否有可用净值数据；false 时各指标字段为 None"
    )
    market_value: DecimalStr | None = Field(
        default=None, description="当前持仓市值（无持仓时为 None）"
    )

    return_20d: float | None = Field(default=None, description="近20个交易日收益率（小数）")
    return_60d: float | None = Field(default=None, description="近60个交易日收益率（小数）")
    return_250d: float | None = Field(default=None, description="近250个交易日收益率（小数）")
    annual_volatility: float | None = Field(default=None, description="年化波动率（日收益标准差×√252）")
    max_drawdown: float | None = Field(default=None, description="区间最大回撤（负数小数）")
    sharpe: float | None = Field(default=None, description="夏普比率（年化，默认无风险利率2%）")
    win_rate: float | None = Field(default=None, description="日收益胜率（正收益日占比，小数）")

    ma20: float | None = Field(default=None, description="20日均线（最新值）")
    ma60: float | None = Field(default=None, description="60日均线（最新值）")
    macd_dif: float | None = Field(default=None, description="MACD DIF（EMA12-EMA26，最新值）")
    macd_dea: float | None = Field(default=None, description="MACD DEA（DIF 的 9 日 EMA，最新值）")
    macd_hist: float | None = Field(
        default=None,
        description="MACD 柱（最新值），口径为 2×(DIF-DEA)，与国内行情软件红绿柱一致",
    )

    trend_signal: str = Field(description="趋势信号：strong_up/up/neutral/down/strong_down")
    trend_reasons: list[str] = Field(default_factory=list, description="信号的可解释理由")


# ---------------------------------------------------------------------------
# 回测
# ---------------------------------------------------------------------------

StrategyType = Literal["buy_hold", "ma_cross", "macd", "dca", "grid"]


class BacktestRequest(ConfiguredBaseModel):
    """回测请求。

    strategy 决定使用哪些参数：
    - buy_hold: 无需额外参数；
    - ma_cross: fast_window / slow_window；
    - macd: macd_fast / macd_slow / macd_signal；
    - dca: invest_interval / invest_amount；
    - grid: grid_step / grid_amount。
    """

    code: str = Field(min_length=1, max_length=32, description="基金代码")
    strategy: StrategyType = Field(description="策略类型")
    initial_capital: float = Field(default=10000.0, gt=0, le=100_000_000, description="初始资金")
    start_date: str | None = Field(default=None, description="回测起始日 YYYY-MM-DD，缺省为最早净值")
    end_date: str | None = Field(default=None, description="回测截止日 YYYY-MM-DD，缺省为最新净值")

    # MA 交叉参数
    fast_window: int = Field(default=20, ge=2, le=120)
    slow_window: int = Field(default=60, ge=5, le=250)
    # MACD 参数
    macd_fast: int = Field(default=12, ge=2, le=60)
    macd_slow: int = Field(default=26, ge=5, le=120)
    macd_signal: int = Field(default=9, ge=2, le=60)
    # 定投参数
    invest_interval: int = Field(default=20, ge=1, le=120, description="每隔多少个交易日投入一次")
    invest_amount: float = Field(default=1000.0, gt=0, le=10_000_000, description="每次定投金额")
    # 网格参数
    grid_step: float = Field(default=0.05, gt=0, le=0.5, description="网格间距（相对基准价，小数）")
    grid_amount: float = Field(default=1000.0, gt=0, le=10_000_000, description="每格交易金额")


class EquityPoint(ConfiguredBaseModel):
    """净值曲线上的一个点。"""

    date: str
    value: float = Field(description="组合总市值（现金+持仓市值）")


class TradeSignal(ConfiguredBaseModel):
    """回测过程中的一笔交易信号（仅研究展示，不会真实下单）。"""

    date: str
    action: Literal["buy", "sell"]
    price: float
    shares: float
    amount: float = Field(description="交易金额（正数）")
    reason: str = Field(description="信号说明，便于解释")


class BacktestResult(ConfiguredBaseModel):
    """回测结果（响应）。"""

    code: str
    name: str
    strategy: StrategyType
    params: dict[str, float | int | str] = Field(default_factory=dict, description="实际生效的策略参数")
    start_date: str
    end_date: str
    initial_capital: float
    final_value: float

    total_return: float | None = Field(default=None, description="区间总收益率（小数）")
    annual_return: float | None = Field(default=None, description="年化收益率（按 252 交易日折算）")
    max_drawdown: float | None = Field(default=None, description="最大回撤（负数小数）")
    sharpe: float | None = Field(default=None, description="夏普比率（年化）")
    trade_count: int = Field(description="成交信号数")

    curve: list[EquityPoint] = Field(default_factory=list, description="资金曲线（按周抽样，规模受控）")
    signals: list[TradeSignal] = Field(default_factory=list, description="交易信号列表（最多100条）")


# ---------------------------------------------------------------------------
# 组合指标摘要与研究信号
# ---------------------------------------------------------------------------


class HoldingMetrics(ConfiguredBaseModel):
    """单个持仓的组合视角指标。"""

    code: str
    name: str
    market_value: DecimalStr
    weight: float | None = Field(default=None, description="占组合总市值比例（小数）")
    trend_signal: str | None = Field(default=None, description="该基金的趋势信号")
    return_20d: float | None = None
    return_60d: float | None = None
    max_drawdown: float | None = None


class ResearchSignal(ConfiguredBaseModel):
    """一条可解释的研究信号（不构成投资建议）。"""

    category: Literal["concentration", "trend", "drawdown"]
    level: Literal["info", "warning", "risk"]
    message: str = Field(description="人类可读的说明文字")


class PortfolioMetricsSummary(ConfiguredBaseModel):
    """组合指标摘要（响应）。"""

    total_market_value: DecimalStr
    position_count: int
    concentration_top1: float | None = Field(default=None, description="第一大持仓权重（小数）")
    concentration_top3: float | None = Field(default=None, description="前三大持仓权重合计（小数）")
    hhi: float | None = Field(default=None, description="赫芬达尔集中度指数 Σw²（小数）")

    # ---- 当前权重回溯组合指标（近一年）----
    methodology: str = Field(
        default="",
        description="回溯组合构造方法说明（权重对齐方式、缺测处理、收益口径）",
    )
    as_of: str | None = Field(default=None, description="指标计算使用的最新净值日期 YYYY-MM-DD")
    total_return_rate: float | None = Field(default=None, description="回溯组合区间总收益率（小数）")
    annualized_return: float | None = Field(default=None, description="回溯组合年化收益率（按252交易日折算）")
    annualized_volatility: float | None = Field(
        default=None, description="回溯组合年化波动率（日收益标准差×√252）"
    )
    max_drawdown: float | None = Field(default=None, description="回溯组合最大回撤（负数小数）")
    sharpe_ratio: float | None = Field(default=None, description="回溯组合夏普比率（年化，无风险利率2%）")
    win_rate: float | None = Field(default=None, description="回溯组合日收益胜率（小数）")

    holdings: list[HoldingMetrics] = Field(default_factory=list)
    signals: list[ResearchSignal] = Field(default_factory=list, description="可解释研究信号")


# ---------------------------------------------------------------------------
# 综合研究信号（独立接口 /quant/signals）
# ---------------------------------------------------------------------------

SignalCategory = Literal[
    "concentration",
    "trend",
    "momentum",
    "drawdown",
    "stock_exposure",
    "overlap",
    "industry",
    "news",
    "market",
]
SignalLevel = Literal["info", "warning", "risk"]


class SignalFilters(ConfiguredBaseModel):
    """综合研究信号的过滤条件（全部为可选，分页参数有默认值）。"""

    category: SignalCategory | None = Field(default=None, description="按信号类别过滤")
    level: SignalLevel | None = Field(default=None, description="按风险级别过滤")
    limit: int = Field(default=100, ge=1, le=1000, description="返回的最大条数")
    offset: int = Field(default=0, ge=0, description="分页偏移")


class ResearchSignalItem(ConfiguredBaseModel):
    """一条带证据与溯源信息的综合研究信号（不构成投资建议）。"""

    category: SignalCategory
    level: SignalLevel
    message: str = Field(description="人类可读的说明文字")
    scope: str = Field(
        default="portfolio",
        description="信号作用范围：portfolio（组合级）/ fund（单基金）/ market（市场级）",
    )
    related_codes: list[str] = Field(default_factory=list, description="关联的基金/股票/指数代码")
    evidence: dict = Field(default_factory=dict, description="支撑信号的结构化证据数据")
    as_of: str = Field(description="信号计算所用数据的截止日期 ISO 字符串")
    source: str = Field(description="数据来源标识，如 fund_nav / fund_holdings / news_items")


class SignalListResponse(ConfiguredBaseModel):
    """综合研究信号列表响应（含分页元信息）。"""

    total: int = Field(description="满足过滤条件的信号总数")
    limit: int
    offset: int
    as_of: str = Field(description="本次计算的统一基准时间 ISO 字符串")
    signals: list[ResearchSignalItem] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# 规则模型筛选器（/quant/screener/*）
# ---------------------------------------------------------------------------

ScreenerMarket = Literal[
    "us_nasdaq",
    "us_spx",
    "hk_tech",
    "hk",
    "cn_300",
    "cn",
    "gold",
    "bond",
    "money",
    "overseas",
]


class ScreenerRequest(ConfiguredBaseModel):
    """规则模型筛选请求。

    不传 codes 时默认使用当前持仓基金作为候选池；
    入选上限 10 只，单基金目标权重 ≤25%，单一市场 ≤50%。
    """

    codes: list[str] | None = Field(
        default=None, max_length=200, description="候选基金代码；缺省为全部持仓基金"
    )
    top_n: int = Field(
        default=10,
        ge=1,
        le=10,
        description="目标组合最大配置基金数（仅限制权重分配，不限制参与分析的样本数）",
    )
    min_samples: int = Field(default=120, ge=20, le=750, description="入选所需最少净值样本")


class ScreenerItem(ConfiguredBaseModel):
    """一只完成分析的候选基金的五档信号与目标权重（仅研究用途，不会真实调仓）。

    所有样本满足的候选都会出现在 items 中；其中综合分前 top_n 只进入目标组合
    并分配非零 target_weight，其余仅参与分析、target_weight 为 0。
    """

    code: str
    name: str
    market: ScreenerMarket
    benchmark: str | None = Field(default=None, description="匹配的市场指数代码，如 IXIC/CSI300")
    score: float = Field(description="综合分：0.45×z(动量)+0.35×z(风险调整动量)+0.20×趋势+0.50×z(回撤)")
    quantile: float | None = Field(default=None, description="同类市场内综合分分位数 ∈[0,1]")
    tier: int = Field(description="五档：+2/+1/0/−1/−2（已含市场状态过滤）")
    label: str = Field(description="五档中文标签，如「值得研究加仓」")
    target_weight: float = Field(description="目标权重（小数，受 25%/50% 约束截断；仅分析标的为 0）")
    in_target: bool = Field(
        description="是否进入目标组合（综合分前 top_n 只分配权重；其余仅参与分析）"
    )
    reasons: list[str] = Field(default_factory=list, description="可解释理由")
    factors: dict[str, float | None] = Field(
        default_factory=dict,
        description="原始因子：momentum / risk_adjusted_momentum_60d / trend / drawdown_120d",
    )
    data_date: str = Field(description="因子计算使用的最新净值日期 YYYY-MM-DD")
    warnings: list[str] = Field(default_factory=list, description="单项提示，如权重被约束截断")


class ScreenerResponse(ConfiguredBaseModel):
    """规则模型筛选响应。"""

    as_of: str | None = Field(default=None, description="本次筛选的统一数据基准日 YYYY-MM-DD")
    methodology: str = Field(description="模型方法说明")
    candidate_count: int = Field(description="样本满足的候选基金数（权益市场，参与横截面排名）")
    excluded_count: int = Field(description="因样本不足被剔除的基金数")
    observe_count: int = Field(description="观察池（黄金/债券/货币/其他海外）基金数，不参与排名")
    selected_count: int = Field(description="参与分析的基金数（= items 长度，即全部样本满足的候选）")
    allocation_count: int = Field(
        description="进入目标组合的基金数（综合分前 top_n 只，target_weight > 0）"
    )
    items: list[ScreenerItem] = Field(
        default_factory=list,
        description="全部完成分析的候选基金：目标组合标的按目标权重降序在前，仅分析标的按综合分降序在后",
    )
    warnings: list[str] = Field(default_factory=list, description="全局提示，如指数数据不足")


# ---------------------------------------------------------------------------
# Walk-Forward 组合回测（/quant/walkforward）
# ---------------------------------------------------------------------------


class WalkForwardWindow(ConfiguredBaseModel):
    """滚动窗口参数：train_window 个净值样本训练、test_window 个样本样本外测试、
    每 step 个样本向前滚动一次（step ≥ test_window，样本外测试区间互不重叠，
    避免同一交易日收益被重复累计）。"""

    train_window: int = Field(default=120, ge=20, le=500, description="训练（因子打分）窗口，净值样本数")
    test_window: int = Field(default=20, ge=5, le=250, description="样本外测试窗口，净值样本数")
    step: int = Field(default=20, ge=1, le=250, description="每次向前滚动的样本数（默认与测试窗口等长）")

    @model_validator(mode="after")
    def _check_step_not_below_test_window(self) -> "WalkForwardWindow":
        if self.step < self.test_window:
            raise ValueError(
                f"step（{self.step}）必须 ≥ test_window（{self.test_window}）："
                "更小的步长会使样本外测试区间重叠、收益被重复累计"
            )
        return self


class WalkForwardRequest(ConfiguredBaseModel):
    """Walk-Forward 组合回测请求。

    - candidate_codes 为候选基金池；缺省时回退为当前持仓基金（数据不足时报错）；
    - 每个滚动窗口：用前 train_window 个净值样本打分（动量 - 回撤惩罚，或外部
      quant_screener 模块），选出 top_n 只等权持有 test_window 个样本后调仓；
    - 基准为全部候选基金等权买入持有；
    - 不卖空、不计手续费、现金零收益；净值缺失日期按前值对齐（当日收益记 0）。
    """

    candidate_codes: list[str] | None = Field(
        default=None,
        min_length=2,
        max_length=250,
        description="候选基金代码池；缺省时使用当前持仓基金",
    )
    window: WalkForwardWindow = Field(default_factory=WalkForwardWindow)
    top_n: int = Field(default=3, ge=1, le=20, description="每期入选基金数（等权）")
    initial_capital: float = Field(default=10000.0, gt=0, le=100_000_000, description="初始资金")
    start_date: str | None = Field(default=None, description="回测起始日 YYYY-MM-DD，缺省为最早净值")
    end_date: str | None = Field(default=None, description="回测截止日 YYYY-MM-DD，缺省为最新净值")


class WalkForwardCurvePoint(ConfiguredBaseModel):
    """抽样曲线上的一个点（策略与基准同日期对齐）。"""

    date: str
    strategy: float = Field(description="策略净值（初始为 1）")
    benchmark: float = Field(description="基准净值（候选池等权买入持有，初始为 1）")


class WalkForwardSegment(ConfiguredBaseModel):
    """一个滚动窗口（训练期 + 样本外测试期）的明细。"""

    index: int = Field(description="窗口序号，从 1 开始")
    train_start: str = Field(description="训练窗口首个净值日期")
    train_end: str = Field(description="训练窗口最后净值日期（打分基准日）")
    test_start: str = Field(description="测试窗口首个日期")
    test_end: str = Field(description="测试窗口最后日期")
    holdings: dict[str, float] = Field(
        default_factory=dict, description="本期目标权重 {基金代码: 权重}，不卖空且合计 ≤ 1"
    )
    segment_return: float | None = Field(default=None, description="本期策略收益（小数）")
    benchmark_return: float | None = Field(default=None, description="本期基准收益（小数）")


class WalkForwardSummary(ConfiguredBaseModel):
    """一组净值序列（策略或基准）的汇总指标。"""

    total_return: float | None = Field(default=None, description="区间总收益率（小数）")
    annual_return: float | None = Field(default=None, description="年化收益率（按 252 交易日折算）")
    max_drawdown: float | None = Field(default=None, description="最大回撤（负数小数）")
    sharpe: float | None = Field(default=None, description="夏普比率（年化，无风险利率 2%）")
    win_rate: float | None = Field(default=None, description="日收益胜率（正收益日占比，小数）")


class WalkForwardResult(ConfiguredBaseModel):
    """Walk-Forward 组合回测结果（响应）。"""

    params: dict[str, float | int | str | list[str]] = Field(
        default_factory=dict, description="实际生效的回测参数"
    )
    start_date: str = Field(description="首个样本外测试日期（净值曲线起点）")
    end_date: str = Field(description="最后净值日期")
    initial_capital: float

    strategy: WalkForwardSummary
    benchmark: WalkForwardSummary
    excess_return: float | None = Field(
        default=None, description="超额收益：策略总收益 - 基准总收益（小数）"
    )
    turnover: float = Field(
        default=0.0,
        description="平均每次调仓换手率（Σ|目标权重-漂移权重|/2 的调仓期均值，小数）",
    )
    rebalance_count: int = Field(description="实际调仓次数")

    curve: list[WalkForwardCurvePoint] = Field(
        default_factory=list, description="策略/基准净值曲线（均匀抽样，规模受控）"
    )
    segments: list[WalkForwardSegment] = Field(default_factory=list, description="滚动窗口明细")

    methodology: str = Field(default="", description="回测方法说明（窗口、打分、对齐、约束）")
    warnings: list[str] = Field(
        default_factory=list, description="数据或参数层面的提示（样本不足、净值缺失等）"
    )


# ---------------------------------------------------------------------------
# 规则参数优化（/quant/optimize）
# ---------------------------------------------------------------------------


class OptimizeFactorWeightGrid(ConfiguredBaseModel):
    """综合分因子权重的搜索网格（各维度取有限候选值，笛卡尔积组成权重组）。

    缺省值仅做轻微扰动，覆盖「动量优先 / 回撤优先 / 趋势优先 / 均衡」四种
    典型取向；某维度只给一个值即固定该维度。
    """

    momentum: list[float] = Field(
        default=[0.45, 0.65], min_length=1, max_length=4,
        description="z(动量) 权重候选",
    )
    risk_adjusted: list[float] = Field(
        default=[0.35, 0.15], min_length=1, max_length=4,
        description="z(风险调整动量) 权重候选",
    )
    trend: list[float] = Field(
        default=[0.20, 0.40], min_length=1, max_length=4,
        description="趋势权重候选",
    )
    drawdown: list[float] = Field(
        default=[0.50, 0.20], min_length=1, max_length=4,
        description="z(回撤) 权重候选",
    )


class OptimizeSearchSpace(ConfiguredBaseModel):
    """规则参数优化的有限搜索网格（各维度笛卡尔积，受 max_trials 截断）。

    - windows: (train_window, test_window) 窗口组合，训练内 purged
      walk-forward 的 step 固定等于 test_window，embargo 固定等于 test_window；
    - factor_weights: 四个因子权重维度的候选值（见 OptimizeFactorWeightGrid）；
    - rebalance_intervals: 调仓间隔（每多少个交易日重新打分调仓一次）；
    - top_n: 每期入选基金数候选；
    - score_thresholds: 综合分入选阈值候选（null 表示不设阈值）。
    """

    windows: list[tuple[int, int]] = Field(
        default=[(120, 20), (90, 20), (150, 30)],
        min_length=1,
        max_length=6,
        description="(训练窗口, 测试窗口) 候选组合，单位：净值样本数",
    )
    factor_weights: OptimizeFactorWeightGrid = Field(default_factory=OptimizeFactorWeightGrid)
    rebalance_intervals: list[int] = Field(
        default=[10, 20, 40, 60], min_length=1, max_length=6,
        description="调仓间隔候选（交易日）",
    )
    top_n: list[int] = Field(
        default=[5, 10, 15, 20], min_length=1, max_length=6,
        description="每期入选基金数候选",
    )
    score_thresholds: list[float | None] = Field(
        default=[None, 0.0, 0.5], min_length=1, max_length=6,
        description="综合分入选阈值候选；null 表示不设阈值",
    )


class OptimizeRequest(ConfiguredBaseModel):
    """规则参数优化请求。

    数据按时间先后 60%/20%/20% 切分为训练/验证/完全留出测试三段；
    在训练段内做 purged walk-forward（段间 embargo = test_window）对
    有限网格中的参数组合打分（max_trials 控制试验数），在验证段比较
    选出最佳参数，最后在完全留出测试段只做一次评估并判定上线门槛。
    """

    candidate_codes: list[str] | None = Field(
        default=None,
        min_length=2,
        max_length=250,
        description="候选基金代码池；缺省时使用当前持仓基金",
    )
    start_date: str | None = Field(default=None, description="数据起始日 YYYY-MM-DD，缺省为最早净值")
    end_date: str | None = Field(default=None, description="数据截止日 YYYY-MM-DD，缺省为最新净值")
    search_space: OptimizeSearchSpace = Field(default_factory=OptimizeSearchSpace)
    max_trials: int = Field(
        default=40, ge=1, le=200,
        description="最大试验（参数组合）数，用于控制运行时间；网格超出时确定式截断",
    )

    # ---- 上线门槛（基于完全留出测试段的单次评估）----
    gate_min_sharpe: float = Field(
        default=0.5, ge=-10.0, le=10.0,
        description="上线门槛：完全留出测试段夏普比率下限",
    )
    gate_max_drawdown: float = Field(
        default=-0.25, ge=-1.0, le=0.0,
        description="上线门槛：完全留出测试段最大回撤下限（负数小数，如 -0.25 表示回撤不得差于 -25%）",
    )
    gate_min_excess_return: float = Field(
        default=0.0, ge=-1.0, le=10.0,
        description="上线门槛：完全留出测试段超额收益下限（小数）",
    )
    gate_max_turnover: float = Field(
        default=1.0, gt=0.0, le=5.0,
        description="上线门槛：完全留出测试段平均每次调仓换手率上限（小数）",
    )

    @model_validator(mode="after")
    def _check_window_bounds(self) -> "OptimizeRequest":
        for train_window, test_window in self.search_space.windows:
            if not (20 <= train_window <= 500):
                raise ValueError(f"训练窗口 {train_window} 超出允许范围 [20, 500]")
            if not (5 <= test_window <= 250):
                raise ValueError(f"测试窗口 {test_window} 超出允许范围 [5, 250]")
        return self


class OptimizeParamSet(ConfiguredBaseModel):
    """一组规则参数（一次试验 / 最佳参数）。"""

    train_window: int = Field(description="训练（因子打分）窗口，净值样本数")
    test_window: int = Field(description="训练内 walk-forward 的样本外测试窗口（= embargo）")
    rebalance_interval: int = Field(description="调仓间隔（交易日）")
    top_n: int = Field(description="每期入选基金数")
    score_threshold: float | None = Field(default=None, description="综合分入选阈值；null 表示不设阈值")
    factor_weights: dict[str, float] = Field(
        default_factory=dict,
        description="因子权重：momentum / risk_adjusted / trend / drawdown",
    )


class OptimizeTrialSummary(ConfiguredBaseModel):
    """一次试验（参数组合）在训练段 purged walk-forward 上的摘要。"""

    trial_index: int = Field(description="试验序号（网格确定式顺序，从 1 开始）")
    params: OptimizeParamSet
    sharpe: float | None = Field(default=None, description="训练段样本外夏普比率（年化）")
    max_drawdown: float | None = Field(default=None, description="训练段样本外最大回撤（负数小数）")
    benchmark_max_drawdown: float | None = Field(default=None, description="训练段基准最大回撤（负数小数）")
    drawdown_improvement: float | None = Field(
        default=None, description="回撤改善：策略回撤 - 基准回撤（正数表示优于基准）"
    )
    excess_return: float | None = Field(default=None, description="超额收益：策略总收益 - 基准总收益（小数）")
    turnover: float = Field(default=0.0, description="训练段平均每次调仓换手率（小数）")
    score: float = Field(
        description="综合评分：0.35×夏普分位 + 0.30×回撤改善分位 + 0.20×超额分位 + 0.15×低换手分位（∈[0,1]）"
    )


class OptimizeEvaluation(ConfiguredBaseModel):
    """一组参数在某一数据段（验证段 / 完全留出测试段）上的单次评估结果。"""

    segment: Literal["validation", "holdout"] = Field(description="评估的数据段")
    start_date: str = Field(description="该段首个净值日期")
    end_date: str = Field(description="该段最后净值日期")
    sample_count: int = Field(description="该段净值样本数")
    rebalance_count: int = Field(description="实际调仓次数")
    strategy: WalkForwardSummary
    benchmark: WalkForwardSummary
    excess_return: float | None = Field(default=None, description="超额收益：策略总收益 - 基准总收益（小数）")
    drawdown_improvement: float | None = Field(
        default=None, description="回撤改善：策略回撤 - 基准回撤（正数表示优于基准）"
    )
    turnover: float = Field(default=0.0, description="平均每次调仓换手率（小数）")


class OptimizeGateStatus(ConfiguredBaseModel):
    """上线门槛判定（各项门槛与默认值均可由请求覆盖）。"""

    min_oos_sharpe: float = Field(description="样本外夏普下限")
    max_drawdown_limit: float = Field(description="最大回撤下限（负数小数，回撤不得差于该值）")
    min_excess_return: float = Field(description="超额收益下限（小数）")
    max_turnover: float = Field(description="平均换手率上限（小数）")
    sharpe_pass: bool = Field(description="完全留出测试段夏普达到下限")
    drawdown_pass: bool = Field(description="完全留出测试段最大回撤不差于下限")
    excess_pass: bool = Field(description="完全留出测试段超额收益达到下限")
    turnover_pass: bool = Field(description="完全留出测试段平均换手率不高于上限")
    passed: bool = Field(description="四项门槛全部满足（达到上线门槛）")
    reasons: list[str] = Field(default_factory=list, description="各门槛逐项的中文判定说明")


class OptimizeResult(ConfiguredBaseModel):
    """规则参数优化结果（响应）。"""

    candidate_codes: list[str] = Field(default_factory=list, description="实际参与优化的候选基金代码")
    data_start: str = Field(description="对齐后共同交易日首个日期")
    data_end: str = Field(description="对齐后共同交易日最后日期")
    sample_count: int = Field(description="对齐后共同交易日总数")
    splits: dict[str, dict[str, str | int]] = Field(
        default_factory=dict,
        description="train/validation/holdout 三段的日期区间与样本数（60%/20%/20% 按时间切分）",
    )
    max_trials: int
    total_candidates: int = Field(description="搜索网格参数组合总数（截断前）")
    executed_trials: int = Field(description="实际执行的试验数（≤ max_trials）")
    trials: list[OptimizeTrialSummary] = Field(
        default_factory=list, description="全部已执行试验的摘要（按试验序号排列）"
    )
    best_params: OptimizeParamSet | None = Field(
        default=None, description="最佳参数（训练段评分最高者的验证段评估最优组合）"
    )
    validation: OptimizeEvaluation | None = Field(
        default=None, description="最佳参数在验证段（20%）上的评估"
    )
    holdout: OptimizeEvaluation | None = Field(
        default=None, description="最佳参数在完全留出测试段（20%）上的评估，仅评估一次"
    )
    gate: OptimizeGateStatus | None = Field(default=None, description="上线门槛判定（基于完全留出测试段）")
    methodology: str = Field(default="", description="优化方法说明")
    warnings: list[str] = Field(default_factory=list, description="数据或参数层面的提示")


# ---------------------------------------------------------------------------
# 量化验证（/quant/validation 与 /quant/snapshot）
# ---------------------------------------------------------------------------


class ValidationCostModel(ConfiguredBaseModel):
    """交易成本模型参数（缺省：买 0.15%、卖默认 0.5%、7 日内 1.5%）。

    卖出费用基于 lot（每笔买入流水形成的份额批次）按 FIFO 估算：
    各 lot 按买入日至卖出日的持有自然日数确定费率（< short_term_days
    按 short_term_sell_fee_rate，否则 sell_fee_rate），份额加权。
    无 lot 数据（无真实交易流水）时按默认卖出费率并给出提示。
    """

    buy_fee_rate: float = Field(default=0.0015, ge=0.0, le=0.1, description="买入（申购）费率")
    sell_fee_rate: float = Field(default=0.005, ge=0.0, le=0.1, description="卖出（赎回）默认费率")
    short_term_sell_fee_rate: float = Field(
        default=0.015, ge=0.0, le=0.2, description="短期持有卖出费率"
    )
    short_term_days: int = Field(default=7, ge=1, le=365, description="短持判定阈值（自然日，不足该天数按短期费率）")


class ValidationRequest(ConfiguredBaseModel):
    """量化验证请求：as_of 快照下的 walk-forward 样本外验证。

    - candidate_codes 缺省为当前持仓基金；
    - as_of 指定时按 QDII lag2 / 国内 lag1 折算各基金可用净值截止日，
      仅使用该日及之前的数据（复现历史任一交易日的研究视角）；
    - include_costs=true 时在样本外收益上扣除交易费用（见 ValidationCostModel）；
    - trial_count 为该策略历史上评估过的参数组合总数（Deflated Sharpe
      的多重检验修正输入），缺省 1 表示无多重比较。
    """

    candidate_codes: list[str] | None = Field(
        default=None, min_length=2, max_length=250,
        description="候选基金代码池；缺省时使用当前持仓基金",
    )
    as_of: str | None = Field(
        default=None, description="快照基准日 YYYY-MM-DD；缺省使用全部历史数据"
    )
    window: WalkForwardWindow = Field(default_factory=WalkForwardWindow)
    top_n: int = Field(default=3, ge=1, le=20, description="每期入选基金数（等权）")
    rebalance_interval: int = Field(default=1, ge=1, le=60, description="调仓间隔（每多少个测试窗口调仓一次）")
    include_costs: bool = Field(default=True, description="是否扣除交易费用")
    cost_model: ValidationCostModel = Field(default_factory=ValidationCostModel)
    trial_count: int = Field(
        default=1, ge=1, le=10000,
        description="历史评估过的参数组合总数（Deflated Sharpe 多重检验修正输入）",
    )
    bootstrap_resamples: int = Field(
        default=500, ge=100, le=5000, description="White Reality Check 重抽样次数"
    )
    block_length: int | None = Field(
        default=None, ge=1, le=250,
        description="block bootstrap 块长；缺省为 round(√T)",
    )
    seed: int = Field(default=42, description="bootstrap 随机种子（结果确定可复现）")


class ValidationRiskMetrics(ConfiguredBaseModel):
    """样本外风险与收益指标。"""

    total_return: float | None = Field(default=None, description="区间总收益率（小数）")
    annual_return: float | None = Field(default=None, description="年化收益率（按 252 交易日折算）")
    sharpe: float | None = Field(default=None, description="夏普比率（年化，无风险利率 2%）")
    max_drawdown: float | None = Field(default=None, description="最大回撤（负数小数）")
    cvar95: float | None = Field(default=None, description="CVaR95：最差 5% 日收益均值（小数）")
    calmar: float | None = Field(default=None, description="Calmar：年化收益 / |最大回撤|")
    win_rate: float | None = Field(default=None, description="日收益胜率（小数）")


class ValidationPredictiveness(ConfiguredBaseModel):
    """因子预测有效性：Rank IC 与五档收益单调性。"""

    rank_ic_mean: float | None = Field(default=None, description="各调仓期 Rank IC（Spearman）均值")
    rank_ic_count: int = Field(default=0, description="参与计算的期数（前瞻收益无并列的期）")
    quintile_returns: list[float | None] = Field(
        default_factory=list, description="按分数五档分组（低→高）的平均前瞻收益"
    )
    quintile_spread: float | None = Field(default=None, description="Q5-Q1 收益差（小数）")
    quintile_kendall_tau: float | None = Field(default=None, description="组序与组均值的 Kendall tau")
    quintile_monotonic: bool = Field(default=False, description="五档收益是否严格单调递增")


class ValidationRobustness(ConfiguredBaseModel):
    """多重检验与抽样稳健性。"""

    trial_count: int = Field(description="输入的试验总数（多重检验修正）")
    skew: float | None = Field(default=None, description="样本外日收益 Fisher 偏度 γ3")
    kurtosis: float | None = Field(default=None, description="样本外日收益 Fisher 峰度 γ4（正态为 0）")
    sharpe_std: float | None = Field(default=None, description="夏普比率标准误")
    expected_max_sharpe: float | None = Field(default=None, description="trial_count 次试验下的期望最大夏普")
    deflated_sharpe: float | None = Field(default=None, description="DSR：P(观测夏普 > 期望最大夏普) ∈[0,1]")
    reality_check_p: float | None = Field(default=None, description="White Reality Check 近似单边 p 值")
    reality_check_stat: float | None = Field(default=None, description="实际检验统计量（主动收益 log 口径）")
    reality_check_null_mean: float | None = Field(default=None, description="零假设下重抽样统计量均值（应接近 0）")
    bootstrap_resamples: int = Field(description="实际重抽样次数")
    block_length: int = Field(description="block bootstrap 块长")


class ValidationNeighborhood(ConfiguredBaseModel):
    """参数邻域稳定性（top_n 与调仓间隔的 ±1 邻域 + 因子权重 ±0.05 扰动）。"""

    center_sharpe: float | None = Field(default=None, description="中心参数点的样本外夏普")
    neighborhood_quantile: float | None = Field(
        default=None, description="中心夏普在邻域全部取值中的经验分位数 ∈[0,1]，越高越稳健"
    )
    band_low: float | None = Field(default=None, description="邻域夏普带下限（去掉一个最小值后）")
    band_high: float | None = Field(default=None, description="邻域夏普带上限（去掉一个最大值后）")
    neighbor_count: int = Field(default=0, description="参与评估的邻域参数点数（含中心）")
    neighbors: dict[str, float | None] = Field(
        default_factory=dict, description="各邻域参数点的样本外夏普 {参数描述: 夏普}"
    )


class ValidationCostSummary(ConfiguredBaseModel):
    """费用口径与实际扣费摘要。"""

    include_costs: bool
    buy_fee_rate: float
    sell_fee_rate: float
    short_term_sell_fee_rate: float
    short_term_days: int
    total_fee_ratio: float = Field(default=0.0, description="样本外累计扣费占初始净值比例（小数）")
    trade_days: int = Field(default=0, description="发生扣费的交易天数")
    sell_fee_basis: str = Field(
        default="default",
        description="卖出费率依据：lots（真实流水 lot 持有期）/ default（无流水按默认费率）",
    )


class ValidationFundSnapshot(ConfiguredBaseModel):
    """一只基金在 as_of 视角下的数据可用性。"""

    code: str
    name: str
    is_qdii: bool
    lag_days: int = Field(description="数据滞后（交易日数）：QDII 默认 2、国内默认 1")
    latest_nav_date: str | None = Field(default=None, description="库中最新净值日期")
    effective_date: str | None = Field(default=None, description="as_of 视角下实际使用的最后净值日期")


class ValidationResponse(ConfiguredBaseModel):
    """量化验证结果（响应）。"""

    as_of: str = Field(description="实际使用的快照基准日 YYYY-MM-DD（缺省时为数据最新交易日）")
    candidate_codes: list[str] = Field(default_factory=list, description="实际参与验证的候选基金代码")
    start_date: str = Field(description="首个样本外测试日期")
    end_date: str = Field(description="最后样本外日期")
    sample_count: int = Field(description="对齐交易日历长度（含训练段）")
    oos_count: int = Field(description="样本外交易日数")

    strategy: ValidationRiskMetrics
    benchmark: ValidationRiskMetrics
    information_ratio: float | None = Field(
        default=None, description="信息比率：主动收益均值 / 跟踪误差 × √252"
    )
    excess_return: float | None = Field(default=None, description="超额收益：策略总收益 - 基准总收益（小数）")

    predictiveness: ValidationPredictiveness
    robustness: ValidationRobustness
    neighborhood: ValidationNeighborhood
    costs: ValidationCostSummary
    fund_snapshots: list[ValidationFundSnapshot] = Field(default_factory=list)

    methodology: str = Field(default="", description="验证方法说明")
    warnings: list[str] = Field(default_factory=list, description="数据或参数层面的提示")


class SnapshotFundInfo(ConfiguredBaseModel):
    """as_of 快照接口中一只基金的可用日期信息。"""

    code: str
    name: str
    is_qdii: bool
    lag_days: int = Field(description="默认数据滞后（交易日数）：QDII 2、国内 1")
    first_nav_date: str | None = Field(default=None, description="库中最早净值日期")
    latest_nav_date: str | None = Field(default=None, description="库中最新净值日期")
    nav_count: int = Field(default=0, description="净值样本数")
    effective_date: str | None = Field(
        default=None, description="按 as_of 与 lag 折算的可用净值截止日（as_of 缺省时为最新净值日）"
    )


class SnapshotResponse(ConfiguredBaseModel):
    """as_of 可用日期快照（响应）。"""

    as_of: str = Field(description="快照基准日 YYYY-MM-DD（缺省时为数据最新交易日）")
    trade_days: list[str] = Field(
        default_factory=list, description="可用交易日（候选基金净值日期并集，升序，规模受控）"
    )
    trade_day_count: int = Field(description="可用交易日总数")
    truncated: bool = Field(default=False, description="trade_days 是否因规模受控被截断（截断时保留尾部）")
    funds: list[SnapshotFundInfo] = Field(default_factory=list)
