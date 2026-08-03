"""基金发现量化路由：候选池因子榜、双动量、V2 信号/回测与验证。

全部为只读研究能力，不涉及任何实盘下单。
独立于任何进行中的 discovery 路由实现：本文件挂载在
/api/discovery/quant/* 子路径，不修改/覆盖既有 discovery 文件，
避免与候选池（CandidatePool/Member）开发中的工作冲突。
候选池模型尚未注册时，pool_id 相关调用返回 400 并提示改用 codes。
"""

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.schemas.discovery_quant import (
    DiscoveryBacktestV2Request,
    DiscoveryValidationRequest,
    DualMomentumQuery,
    DualMomentumResponse,
    FactorBoardQuery,
    FactorBoardResponse,
    FactorSortField,
)
from app.schemas.quant import ValidationRequest, ValidationResponse
from app.schemas.quant_v2 import BacktestV2Request, BacktestV2Result, SignalsV2Response
from app.services import quant_discovery as discovery_service
from app.services.quant import QuantError

router = APIRouter(prefix="/discovery/quant", tags=["discovery-quant"])


def _split_codes(codes: str | None) -> list[str] | None:
    return [code.strip() for code in codes.split(",") if code.strip()] if codes else None


def _resolve(
    db: Session, pool_id: int | None, codes: str | None
) -> tuple[list[str] | None, list[str]]:
    """解析候选来源（显式 codes 优先于 pool_id）；QuantError 转换为 400。"""
    try:
        return discovery_service.resolve_candidates(db, pool_id, _split_codes(codes))
    except QuantError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except ValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from exc


@router.get("/factors", response_model=FactorBoardResponse)
def factor_leaderboard(
    codes: str | None = Query(default=None, description="候选基金代码，逗号分隔"),
    pool_id: int | None = Query(default=None, description="候选池 ID（需候选池模型可用）"),
    sort: FactorSortField = Query(default="momentum_12_1", description="排序因子"),
    order: str = Query(default="desc", pattern="^(asc|desc)$", description="排序方向"),
    window: int = Query(default=252, ge=20, le=756, description="风险/比率指标回溯窗口（交易日）"),
    limit: int = Query(default=20, ge=1, le=100, description="每页条数"),
    offset: int = Query(default=0, ge=0, description="分页偏移"),
    min_samples: int = Query(default=60, ge=2, le=500, description="入选所需最少净值样本"),
    db: Session = Depends(get_db),
) -> FactorBoardResponse:
    """候选池成员因子榜（分页）。

    输出每只候选的 1m/3m/1y/3y 区间收益、年化波动、最大回撤、夏普、
    索提诺、Calmar、CVaR95、12-1 绝对动量与同类（同市场层）分位，
    按 sort 指定因子排序（缺失值恒排末尾）并分页返回。
    仅为研究排序，不构成投资建议。
    """
    resolved, source_warnings = _resolve(db, pool_id, codes)
    try:
        query = FactorBoardQuery(
            codes=resolved,
            sort=sort,
            order=order,  # type: ignore[arg-type]
            window=window,
            limit=limit,
            offset=offset,
            min_samples=min_samples,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from exc
    try:
        response = discovery_service.factor_leaderboard(db, query)
    except QuantError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except ValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from exc
    response.warnings = source_warnings + response.warnings
    return response


@router.get("/dual-momentum", response_model=DualMomentumResponse)
def dual_momentum(
    codes: str | None = Query(default=None, description="候选基金代码，逗号分隔"),
    pool_id: int | None = Query(default=None, description="候选池 ID（需候选池模型可用）"),
    top_n: int = Query(default=1, ge=1, le=10, description="相对动量入选只数"),
    db: Session = Depends(get_db),
) -> DualMomentumResponse:
    """双动量（Dual Momentum）当期信号。

    相对动量：候选内按 12-1 动量降序取前 top_n 只等权；
    绝对动量：仅动量 > 0 者可入选，前 top_n 全部 ≤ 0 时整体回避
    （hold_offense=false、权重归零）。仅为研究信号，不构成投资建议。
    """
    resolved, source_warnings = _resolve(db, pool_id, codes)
    try:
        response = discovery_service.dual_momentum(
            db, DualMomentumQuery(codes=resolved, top_n=top_n)
        )
    except QuantError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except ValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from exc
    response.warnings = source_warnings + response.warnings
    return response


@router.get("/signals-v2", response_model=SignalsV2Response)
def pool_signals_v2(
    codes: str | None = Query(
        default=None, description="候选基金代码，逗号分隔；与 pool_id 二选一（优先）"
    ),
    pool_id: int | None = Query(default=None, description="候选池 ID（需候选池模型可用）"),
    top_n: int = Query(default=8, ge=1, le=30, description="入选基金数上限"),
    db: Session = Depends(get_db),
) -> SignalsV2Response:
    """候选池稳健组合 V2 当期目标信号（与 /api/quant/v2/signals 同一套逻辑）。

    候选来源为 pool_id 成员或显式 codes（均未提供时回退当前持仓基金）。
    仅为研究信号，不构成投资建议。
    """
    try:
        return discovery_service.pool_signals_v2(db, pool_id, _split_codes(codes), top_n)
    except QuantError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except ValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from exc


@router.post("/backtest-v2", response_model=BacktestV2Result)
def pool_backtest_v2(
    payload: DiscoveryBacktestV2Request, db: Session = Depends(get_db)
) -> BacktestV2Result:
    """候选池稳健组合 V2 月频回测（与 /api/quant/v2/backtest 同一套引擎）。

    候选来源为 pool_id 成员或显式 codes（codes 优先；均未提供时回退当前
    持仓基金）。月频调仓、层内 HRP、波动率目标与冻结保护等口径详见
    响应中的 methodology。仅为研究回测，不产生任何实盘下单行为。
    """
    try:
        req = BacktestV2Request(
            candidate_codes=None,  # 由服务层解析 pool_id / codes 后回填
            start_date=payload.start_date,
            end_date=payload.end_date,
            initial_capital=payload.initial_capital,
            top_n=payload.top_n,
            rebalance_interval_months=payload.rebalance_interval_months,
            target_vol=payload.target_vol,
            max_fund_weight=payload.max_fund_weight,
            max_family_weight=payload.max_family_weight,
            max_qdii_weight=payload.max_qdii_weight,
            fee_model=payload.fee_model,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from exc
    try:
        return discovery_service.pool_backtest_v2(db, payload.pool_id, payload.codes, req)
    except QuantError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except ValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from exc


@router.post("/validation", response_model=ValidationResponse)
def pool_validation(
    payload: DiscoveryValidationRequest, db: Session = Depends(get_db)
) -> ValidationResponse:
    """候选池量化验证（与 /api/quant/validation 同一套 walk-forward 验证）。

    候选来源为 pool_id 成员或显式 codes（codes 优先；均未提供时回退当前
    持仓基金）。样本外风险指标、Rank IC、DSR、White Reality Check 与
    参数邻域稳定性等口径详见响应中的 methodology。仅为研究验证。
    """
    try:
        req = ValidationRequest(
            candidate_codes=None,  # 由服务层解析 pool_id / codes 后回填
            as_of=payload.as_of,
            window=payload.window,
            top_n=payload.top_n,
            rebalance_interval=payload.rebalance_interval,
            include_costs=payload.include_costs,
            cost_model=payload.cost_model,
            trial_count=payload.trial_count,
            bootstrap_resamples=payload.bootstrap_resamples,
            block_length=payload.block_length,
            seed=payload.seed,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from exc
    try:
        return discovery_service.pool_validation(db, payload.pool_id, payload.codes, req)
    except QuantError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except ValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from exc
