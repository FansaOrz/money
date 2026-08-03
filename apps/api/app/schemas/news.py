"""资讯（News）相关 Schema。"""

from datetime import datetime

from pydantic import Field

from app.schemas.common import ConfiguredBaseModel


class NewsItemOut(ConfiguredBaseModel):
    """单条资讯（响应）。"""

    id: int
    source: str
    title: str
    summary: str | None
    url: str | None
    published_at: datetime | None
    related_codes: list[str] = Field(
        default_factory=list, description="关联标的代码列表；市场快讯为空"
    )
    fetched_at: datetime


class NewsSyncStatus(ConfiguredBaseModel):
    """最近一次同步状态摘要。"""

    synced_at: datetime | None = None
    fetched: int = Field(default=0, description="抓取到的条目数")
    inserted: int = Field(default=0, description="新增入库条目数")
    skipped: int = Field(default=0, description="去重跳过条目数")
    degraded: bool = Field(default=False, description="是否处于降级（数据源不可用）")
    message: str | None = None


class NewsListResponse(ConfiguredBaseModel):
    """资讯列表（响应）。

    scope=related 时仅返回与当前持仓基金相关的资讯；
    scope=market 时返回全局市场快讯。
    """

    scope: str = Field(description="related | market")
    items: list[NewsItemOut]
    total: int
    # 最近一次同步结果摘要，前端可据此提示数据可能为空
    last_sync: NewsSyncStatus | None = None


class NewsSyncResult(NewsSyncStatus):
    """手动触发同步的结果（响应）。"""

    errors: list[str] = Field(default_factory=list)
