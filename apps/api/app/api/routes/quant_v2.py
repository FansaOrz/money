"""稳健组合策略 V2 路由：月频动量 + 层内 HRP + 波动率目标回测与当期信号。

全部为只读研究能力，不涉及任何实盘下单。
与 v1 路由（/api/quant/*）相互独立，挂载在 /api/quant/v2/* 子路径。
"""

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.schemas.quant_v2 import BacktestV2Request, BacktestV2Result, SignalsV2Response
from app.services import quant_v2 as v2_service
from app.services.quant import QuantError

router = APIRouter(prefix="/quant/v2", tags=["quant-v2"])


@router.post("/backtest", response_model=BacktestV2Result)
def backtest_v2(payload: BacktestV2Request, db: Session = Depends(get_db)) -> BacktestV2Result:
    """稳健组合 V2 月频回测。

    每月最后一个交易日打分（绝对动量 12-1 > 0、同家族份额去重、层内前 30%），
    层内 HRP 配置（失败回退逆波动/等权），单基金 8% / 家族 10% / QDII 30% 约束，
    EWMA60 波动目标 10%（只降仓），高波动+急反弹冻结调仓。
    信号日 T 收盘打分，T+1 按当日净值成交（目标含 QDII 时统一 T+2）。
    费用模型接口预留（默认零费用）。仅为研究回测，不产生任何实盘下单行为。
    """
    try:
        return v2_service.run_backtest_v2(db, payload)
    except QuantError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


@router.get("/signals", response_model=SignalsV2Response)
def signals_v2(
    codes: str | None = Query(default=None, description="候选基金代码，逗号分隔；缺省为当前持仓基金"),
    top_n: int = Query(default=8, ge=1, le=30, description="入选基金数上限"),
    db: Session = Depends(get_db),
) -> SignalsV2Response:
    """稳健组合 V2 当期目标信号（基于最新净值）。

    与回测共用同一套打分/配置/约束逻辑；输出入选基金的目标权重、
    动量排名、波动率目标系数与冻结状态。仅为研究信号，不构成投资建议。
    """
    candidate_codes = (
        [code.strip() for code in codes.split(",") if code.strip()] if codes else None
    )
    try:
        req = BacktestV2Request(candidate_codes=candidate_codes, top_n=top_n)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from exc
    try:
        return v2_service.current_signals(db, req)
    except QuantError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
