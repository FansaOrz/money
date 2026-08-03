"""基金发现（全市场目录 + 候选池）相关 Schema。"""

from datetime import datetime
from typing import Any

from pydantic import Field

from app.schemas.common import ConfiguredBaseModel


# ---------------------------------------------------------------------------
# 全市场基金目录
# ---------------------------------------------------------------------------


class CatalogSyncRequest(ConfiguredBaseModel):
    """目录同步请求。"""

    refresh_active: bool = Field(
        default=False, description="是否调用 fund_open_fund_daily_em 刷新 active 状态"
    )
    mark_inactive: bool = Field(
        default=False,
        description="refresh_active 成功时，是否把不在当日净值列表中的基金标记为不活跃",
    )


class CatalogSyncResult(ConfiguredBaseModel):
    """目录同步结果。"""

    total_rows: int = Field(description="数据源返回行数")
    inserted: int
    updated: int
    active_marked: int = 0
    inactive_marked: int = 0
    catalog_size: int = Field(description="同步后目录总量")


class CatalogEntryOut(ConfiguredBaseModel):
    """目录条目（响应）。"""

    code: str
    name: str
    pinyin_abbr: str | None = None
    fund_type: str | None = None
    market: str | None = None
    family: str | None = None
    share_class: str | None = None
    active: bool


class CatalogListResponse(ConfiguredBaseModel):
    """目录分页列表（响应）。"""

    items: list[CatalogEntryOut]
    total: int
    limit: int
    offset: int


class CatalogStats(ConfiguredBaseModel):
    """目录统计（响应）。"""

    total: int
    active: int
    inactive: int
    by_type: dict[str, int]
    by_market: dict[str, int]


# ---------------------------------------------------------------------------
# 候选池
# ---------------------------------------------------------------------------


class PoolBuildRequest(ConfiguredBaseModel):
    """建池请求。只建池，不触发全历史净值回填。"""

    name: str | None = Field(default=None, description="池名称，缺省自动生成")
    max_size: int = Field(
        default=800, ge=1, description="核心池规模上限，服务端钳制在 500~1000"
    )
    only_active: bool = Field(default=True, description="是否只从活跃基金中筛选")
    exclude_keywords: list[str] | None = Field(
        default=None, description="按名称剔除的关键词（如 联接/定开），缺省用内置列表"
    )


class PoolMemberOut(ConfiguredBaseModel):
    """候选池成员（响应）。"""

    code: str
    name: str
    fund_type: str | None = None
    market: str | None = None
    family: str | None = None
    share_class: str | None = None
    tier: int = Field(description="1 核心权益 / 2 次级权益 / 3 观察")
    rank: int
    status: str
    nav_samples: int
    nav_ready: bool


class PoolSummary(ConfiguredBaseModel):
    """池概要统计。"""

    tier_counts: dict[str, int]
    market_counts: dict[str, int]
    nav_ready_count: int


class PoolOut(ConfiguredBaseModel):
    """候选池概要（响应）。"""

    id: int
    name: str
    max_size: int
    status: str
    member_count: int
    notes: str | None = None
    created_at: datetime


class PoolDetail(PoolOut):
    """候选池详情（含成员与统计）。"""

    params: dict[str, Any] | None = None
    summary: PoolSummary
    members: list[PoolMemberOut]


class PoolListResponse(ConfiguredBaseModel):
    """候选池列表（响应）。"""

    items: list[PoolOut]
    total: int
