"""组合（Portfolio）相关 Schema。"""

from decimal import Decimal

from pydantic import Field

from app.schemas.common import ConfiguredBaseModel, DecimalStr


class PositionItem(ConfiguredBaseModel):
    """单个持仓条目（响应）。"""

    account_id: int
    account_name: str
    instrument_id: int
    instrument_code: str
    instrument_name: str
    shares: DecimalStr
    cost: DecimalStr
    cost_price: DecimalStr | None
    nav: DecimalStr | None
    nav_date: str | None
    # 最新市值，可能为空（尚无行情）
    market_value: DecimalStr | None
    # 浮动盈亏 = 市值 - 成本（无市值时为 None）
    profit: DecimalStr | None
    # 收益率（小数，例如 0.0523 表示 5.23%），无市值时为 None
    profit_rate: DecimalStr | None


class PortfolioSummary(ConfiguredBaseModel):
    """组合汇总（响应）。"""

    total_cost: DecimalStr = Field(description="总成本")
    total_market_value: DecimalStr = Field(description="总市值（无市值时回退为成本）")
    total_profit: DecimalStr = Field(description="总盈亏")
    # 整体收益率（小数），总成本为 0 时为 None
    profit_rate: DecimalStr | None
    total_return_rate: DecimalStr | None = None
    snapshot_date: str | None = None
    estimated_return: DecimalStr | None = None
    estimated_return_rate: DecimalStr | None = None
    year_return: DecimalStr | None = None
    previous_year_return: DecimalStr | None = None
    position_count: int = Field(description="持仓条目数")
    currency: str = "CNY"


class PortfolioSnapshotItem(ConfiguredBaseModel):
    snapshot_date: str
    total_cost: DecimalStr
    total_market_value: DecimalStr
    total_profit: DecimalStr


class NavSyncResult(ConfiguredBaseModel):
    total_funds: int
    updated: int
    failed: int
    latest_nav_date: str | None
    snapshot_date: str | None


class PositionListResponse(ConfiguredBaseModel):
    """持仓列表（响应）。"""

    items: list[PositionItem]
    total: int


class FundReturnItem(ConfiguredBaseModel):
    """单只基金在一个窗口内的收益（按当前份额估算）。"""

    instrument_id: int
    instrument_code: str
    instrument_name: str
    is_qdii: bool = Field(description="是否 QDII（按名称识别）")
    shares: DecimalStr = Field(description="当前持有份额")
    # 收益金额 = shares * (unit_nav_end - unit_nav_start)，无数据时为 None
    return_amount: DecimalStr | None
    # 收益率（小数）：现金分红再投资总收益，累计净值缺失区间回退单位净值
    return_rate: DecimalStr | None
    # 实际使用的净值端点（起点取 <= 目标日期的最后一条，终点取最新一条）
    start_date: str | None = Field(description="实际起点净值日期")
    end_date: str | None = Field(description="实际终点净值日期（该基金的最新净值日期）")
    start_nav: DecimalStr | None = None
    end_nav: DecimalStr | None = None
    rate_basis: str | None = Field(
        default=None,
        description=(
            "收益率口径：dividend_reinvested（分红再投资）/"
            "total_return_with_unit_fallback（部分区间回退单位净值）"
        ),
    )
    # available：两端均有净值；stale：一端或两端缺失；approximate：窗口内有份额变动
    status: str = Field(description="available / stale / approximate")
    stale_reason: str | None = Field(default=None, description="stale 时的原因说明")
    has_flows: bool = Field(description="窗口内是否存在 BUY/SELL/REINVEST 流水")
    weight: DecimalStr | None = Field(description="组合内权重（按期末金额加权）")


class PortfolioReturnWindow(ConfiguredBaseModel):
    """一个窗口的组合收益（按各基金期末金额加权）。"""

    window: str = Field(description="窗口标识：1d / 1w / 1m / 3m")
    target_start_date: str = Field(description="目标起点日期")
    # 组合收益金额（仅汇总 available/approximate 基金），全部 stale 时为 None
    return_amount: DecimalStr | None
    # 组合收益率 = 总收益金额 / 总起点金额；无可用基金时为 None
    return_rate: DecimalStr | None
    # coverage = 可用基金期末金额 / 全部基金期末金额（0~1）
    coverage: DecimalStr
    available_count: int = Field(description="status=available 的基金数")
    approximate_count: int = Field(description="status=approximate 的基金数")
    stale_count: int = Field(description="status=stale 的基金数")
    # 参与加权的基金实际净值端点的最大日期（QDII 通常更旧）
    as_of_end_date: str | None = Field(default=None, description="参与加权基金的最晚净值日期")
    items: list[FundReturnItem]


class PortfolioReturnsResponse(ConfiguredBaseModel):
    """组合区间收益响应：一次返回全部窗口，或按 query 指定单窗口。"""

    windows: dict[str, PortfolioReturnWindow]


class SeedPositionRequest(ConfiguredBaseModel):
    """手工录入持仓（开发联调用，临时接口）。

    解析器就绪后将由导入流程替代，输入直接使用 Decimal 保持精度。
    """

    account_name: str = Field(min_length=1, max_length=100)
    instrument_code: str = Field(min_length=1, max_length=32)
    instrument_name: str = Field(min_length=1, max_length=200)
    shares: Decimal = Field(gt=0)
    cost: Decimal = Field(ge=0)
    market_value: Decimal | None = Field(default=None, ge=0)
