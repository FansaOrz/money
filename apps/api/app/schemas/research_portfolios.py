"""统一研究组合 Schema（GET /api/research/portfolios）。

仅包含只读研究能力，不涉及任何实盘下单。
响应结构与前端 web/src/lib/types.ts 中已有的 ResearchPortfoliosResponse /
ResearchPortfolio / ResearchPortfolioHolding 保持兼容：
- 顶层：portfolios / as_of / warnings（前端同时兼容 items/results 包裹形态）；
- 组合：id / name / kind / description / methodology / as_of / status / holdings；
- 持仓：code / name / weight / score（可空）/ reason / reasons / market。
"""

from typing import Literal

from pydantic import Field

from app.schemas.common import ConfiguredBaseModel


class ResearchPortfolioHolding(ConfiguredBaseModel):
    """研究组合中的单个持仓（仅研究展示，不构成投资建议）。"""

    code: str = Field(description="基金/标的代码")
    name: str = Field(description="基金/标的名称")
    weight: float = Field(description="目标权重（小数）")
    score: float | None = Field(
        default=None, description="打分（如 12-1 动量，小数）；无可靠口径时为 null，不伪造"
    )
    reason: str = Field(default="", description="入选理由摘要（reasons 的拼接形态）")
    reasons: list[str] = Field(default_factory=list, description="可解释理由列表")
    market: str = Field(
        default="", description="市场层分类，如 cn_300 / hk / us / gold / bond / money / overseas"
    )


class ResearchPortfolio(ConfiguredBaseModel):
    """单个研究组合（当前仅基金组合：最新候选池 × 稳健组合 V2 当期信号）。"""

    id: str = Field(description="组合标识（如 fund-v2-pool-3）")
    name: str = Field(description="组合名称")
    kind: Literal["fund", "stock"] = Field(description="组合类别：fund 基金 / stock 股票")
    status: Literal["research_only"] = Field(
        default="research_only",
        description="固定为 research_only：仅研究用途，不构成投资建议、不产生任何订单",
    )
    description: str = Field(default="", description="组合简介")
    methodology: str = Field(default="", description="策略方法说明")
    as_of: str | None = Field(default=None, description="信号基准日（最新净值日期）YYYY-MM-DD")
    holdings: list[ResearchPortfolioHolding] = Field(
        default_factory=list, description="组合持仓（数据不足时为空数组，不返回 404）"
    )


class ResearchPortfoliosResponse(ConfiguredBaseModel):
    """统一研究组合列表（响应）。

    数据不足时不返回 404：portfolios 为空数组或组合 holdings 为空，
    并在顶层 warnings 说明原因。
    """

    portfolios: list[ResearchPortfolio] = Field(
        default_factory=list, description="研究组合列表（无可用数据时为空数组）"
    )
    as_of: str | None = Field(default=None, description="整体信号基准日（取基金组合 as_of）")
    warnings: list[str] = Field(default_factory=list, description="数据或参数层面的提示")
