"""基金发现路由：全市场基金目录同步/查询/统计 + 候选池构建/列表/详情。"""

import json

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.models import FundCatalogEntry
from app.schemas.discovery import (
    CatalogEntryOut,
    CatalogListResponse,
    CatalogStats,
    CatalogSyncRequest,
    CatalogSyncResult,
    PoolBuildRequest,
    PoolDetail,
    PoolListResponse,
    PoolMemberOut,
    PoolOut,
    PoolSummary,
)
from app.services import candidate_pool as pool_service
from app.services import fund_catalog as catalog_service
from app.services.candidate_pool import PoolBuildParams

router = APIRouter(prefix="/discovery", tags=["discovery"])


# ---------------------------------------------------------------------------
# 全市场基金目录
# ---------------------------------------------------------------------------


@router.post("/catalog/sync", response_model=CatalogSyncResult)
def sync_catalog(
    payload: CatalogSyncRequest | None = None,
    db: Session = Depends(get_db),
) -> CatalogSyncResult:
    """同步全市场基金目录（akshare fund_name_em，幂等 upsert）。

    可选 refresh_active=True 时调用 fund_open_fund_daily_em 刷新 active 状态。
    """
    payload = payload or CatalogSyncRequest()
    try:
        result = catalog_service.sync_fund_catalog(
            db,
            refresh_active=payload.refresh_active,
            mark_inactive=payload.mark_inactive,
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc
    return CatalogSyncResult(**result)


@router.get("/catalog", response_model=CatalogListResponse)
def list_catalog(
    keyword: str | None = Query(default=None, description="按代码或名称模糊搜索"),
    fund_type: str | None = Query(default=None, description="按东财基金类型精确过滤"),
    market: str | None = Query(default=None, description="按内部市场分类过滤"),
    active: bool | None = Query(default=None, description="按活跃状态过滤"),
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
) -> CatalogListResponse:
    """分页查询全市场基金目录。"""
    stmt = select(FundCatalogEntry)
    count_stmt = select(func.count(FundCatalogEntry.id))
    if keyword:
        like = f"%{keyword}%"
        condition = FundCatalogEntry.name.like(like) | FundCatalogEntry.code.like(like)
        stmt = stmt.where(condition)
        count_stmt = count_stmt.where(condition)
    if fund_type:
        stmt = stmt.where(FundCatalogEntry.fund_type == fund_type)
        count_stmt = count_stmt.where(FundCatalogEntry.fund_type == fund_type)
    if market:
        stmt = stmt.where(FundCatalogEntry.market == market)
        count_stmt = count_stmt.where(FundCatalogEntry.market == market)
    if active is not None:
        stmt = stmt.where(FundCatalogEntry.active.is_(active))
        count_stmt = count_stmt.where(FundCatalogEntry.active.is_(active))

    total = db.scalar(count_stmt) or 0
    entries = db.scalars(
        stmt.order_by(FundCatalogEntry.code).limit(limit).offset(offset)
    ).all()
    return CatalogListResponse(
        items=[CatalogEntryOut.model_validate(entry) for entry in entries],
        total=int(total),
        limit=limit,
        offset=offset,
    )


@router.get("/catalog/stats", response_model=CatalogStats)
def catalog_stats(db: Session = Depends(get_db)) -> CatalogStats:
    """目录统计：总量、active 分布、按类型/市场分布。"""
    return CatalogStats(**catalog_service.get_catalog_stats(db))


# ---------------------------------------------------------------------------
# 候选池
# ---------------------------------------------------------------------------


@router.post("/pools/build", response_model=PoolDetail, status_code=status.HTTP_201_CREATED)
def build_pool(payload: PoolBuildRequest, db: Session = Depends(get_db)) -> PoolDetail:
    """构建核心候选池：过滤 → 家族去重 → 分层配额 → 落库。

    max_size 默认 800，服务端钳制在 500~1000。
    只建池并标记各成员 nav_ready，不阻塞做全历史净值回填——
    回填由后续任务按池内代码调度，完成后可再查池详情看 nav_ready 变化。
    """
    params = PoolBuildParams(
        max_size=payload.max_size,
        only_active=payload.only_active,
        name=payload.name,
    )
    if payload.exclude_keywords is not None:
        params.exclude_keywords = tuple(payload.exclude_keywords)
    try:
        pool = pool_service.build_candidate_pool(db, params)
    except RuntimeError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    return _pool_detail(pool)


@router.get("/pools", response_model=PoolListResponse)
def list_pools(
    limit: int = Query(default=20, ge=1, le=100),
    db: Session = Depends(get_db),
) -> PoolListResponse:
    """按创建时间倒序列出候选池。"""
    pools = pool_service.list_pools(db, limit=limit)
    return PoolListResponse(items=[_pool_out(pool) for pool in pools], total=len(pools))


@router.get("/pools/{pool_id}", response_model=PoolDetail)
def pool_detail(pool_id: int, db: Session = Depends(get_db)) -> PoolDetail:
    """候选池详情：参数快照、分层/市场统计、成员列表（按 rank 排序）。"""
    pool = pool_service.get_pool(db, pool_id)
    if pool is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"候选池不存在：{pool_id}",
        )
    return _pool_detail(pool)


@router.post("/pools/{pool_id}/refresh-nav", response_model=PoolDetail)
def refresh_pool_nav(pool_id: int, db: Session = Depends(get_db)) -> PoolDetail:
    """净值回填完成后刷新池内成员的 nav_samples / nav_ready 标记。"""
    pool = pool_service.get_pool(db, pool_id)
    if pool is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"候选池不存在：{pool_id}",
        )
    pool_service.refresh_member_nav_status(db, pool_id)
    db.refresh(pool)
    return _pool_detail(pool)


# ---------------------------------------------------------------------------
# 组装
# ---------------------------------------------------------------------------


def _pool_out(pool) -> PoolOut:
    return PoolOut(
        id=pool.id,
        name=pool.name,
        max_size=pool.max_size,
        status=pool.status,
        member_count=pool.member_count,
        notes=pool.notes,
        created_at=pool.created_at,
    )


def _pool_detail(pool) -> PoolDetail:
    try:
        params = json.loads(pool.params) if pool.params else None
    except (TypeError, ValueError):
        params = None
    summary = pool_service.pool_summary(pool)
    return PoolDetail(
        **_pool_out(pool).model_dump(),
        params=params,
        summary=PoolSummary(**summary),
        members=[PoolMemberOut.model_validate(member) for member in pool.members],
    )
