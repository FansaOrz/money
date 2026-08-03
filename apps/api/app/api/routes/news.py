"""资讯相关路由：列表查询与手动同步。"""

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.schemas.news import (
    NewsItemOut,
    NewsListResponse,
    NewsSyncResult,
    NewsSyncStatus,
)
from app.services import news as news_service

router = APIRouter(prefix="/news", tags=["news"])


def _to_out(item) -> NewsItemOut:
    return NewsItemOut(
        id=item.id,
        source=item.source,
        title=item.title,
        summary=item.summary,
        url=item.url,
        published_at=item.published_at,
        related_codes=item.related_codes.split(",") if item.related_codes else [],
        fetched_at=item.fetched_at,
    )


@router.get("", response_model=NewsListResponse)
def list_news(
    scope: str = Query(default="related", pattern="^(related|market)$"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
) -> NewsListResponse:
    """查询资讯列表。

    - scope=related：与当前持仓基金（名称/重仓股关键词）相关的资讯；
    - scope=market：全局市场快讯（无关联标的）。
    """
    items, total = news_service.list_news(db, scope=scope, limit=limit, offset=offset)
    status_data = news_service.get_last_sync_status()
    return NewsListResponse(
        scope=scope,
        items=[_to_out(item) for item in items],
        total=total,
        last_sync=NewsSyncStatus(**status_data) if status_data.get("synced_at") else None,
    )


@router.post("/sync", response_model=NewsSyncResult, status_code=status.HTTP_200_OK)
def sync_news(db: Session = Depends(get_db)) -> NewsSyncResult:
    """手动触发一次资讯同步（抓取 -> 去重入库）。"""
    return NewsSyncResult(**news_service.sync_news(db))
