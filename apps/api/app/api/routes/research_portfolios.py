"""统一研究组合路由：GET /api/research/portfolios。

只读研究能力，不涉及任何实盘下单/订单行为。
基金部分自动选取最新候选池并复用既有 V2 当期信号逻辑；
数据不足时不返回 404，而是返回 200 + 空 portfolios + 顶层 warnings 说明。
"""

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.schemas.research_portfolios import ResearchPortfoliosResponse
from app.services import research_portfolios as research_portfolios_service

router = APIRouter(prefix="/research", tags=["research-portfolios"])


@router.get("/portfolios", response_model=ResearchPortfoliosResponse)
def list_research_portfolios(
    fund_top_n: int = Query(default=8, ge=1, le=30, description="基金组合入选只数上限"),
    db: Session = Depends(get_db),
) -> ResearchPortfoliosResponse:
    """统一研究组合列表（只读）。

    返回基金研究组合（最新候选池 × 稳健组合 V2 当期信号），
    持仓字段为 code/name/weight/score/reason/reasons/market，
    组合固定 status=research_only（仅研究用途，不构成投资建议）。
    最新候选池缺失或研究就绪数据不足时不返回 404：
    portfolios 为空数组（或组合 holdings 为空），顶层 warnings 说明原因。
    股票研究组合数据链路尚未接入，本轮仅以 warning 提示，不伪造数据。
    """
    return research_portfolios_service.list_research_portfolios(db, fund_top_n=fund_top_n)
