"""新闻事件分析与基金影响映射。

原始资讯保留在 ``news_items``；本模块把跨来源的重复报道聚合为事件，
再保存事件对具体基金的可解释影响。页面只读取本地结果，不在请求链路调用大模型。
"""

from datetime import datetime

from sqlalchemy import (
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class NewsEvent(Base):
    """去重后的新闻事件及其结构化分析。"""

    __tablename__ = "news_events"
    __table_args__ = (
        Index("ix_news_events_latest_published_at", "latest_published_at"),
        Index("ix_news_events_expires_at", "expires_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    canonical_key: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    first_published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    latest_published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    event_type: Mapped[str] = mapped_column(String(40), default="other", nullable=False)
    direction: Mapped[str] = mapped_column(String(16), default="neutral", nullable=False)
    impact_level: Mapped[str] = mapped_column(String(16), default="low", nullable=False)
    impact_score: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    horizon_days: Mapped[int] = mapped_column(Integer, default=7, nullable=False)
    confidence: Mapped[float] = mapped_column(Float, default=0.4, nullable=False)
    source_quality: Mapped[float] = mapped_column(Float, default=0.6, nullable=False)
    plain_summary: Mapped[str] = mapped_column(Text, default="", nullable=False)
    facts_json: Mapped[str] = mapped_column(Text, default="[]", nullable=False)
    targets_json: Mapped[str] = mapped_column(Text, default="[]", nullable=False)

    source_count: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    analysis_method: Mapped[str] = mapped_column(String(20), default="rules", nullable=False)
    analysis_model: Mapped[str | None] = mapped_column(String(100))
    analyzed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class NewsEventItem(Base):
    """原始资讯到聚合事件的唯一归属关系。"""

    __tablename__ = "news_event_items"
    __table_args__ = (
        UniqueConstraint("news_item_id", name="uq_news_event_item_news"),
        UniqueConstraint("event_id", "news_item_id", name="uq_news_event_item_pair"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("news_events.id"), index=True)
    news_item_id: Mapped[int] = mapped_column(ForeignKey("news_items.id"), index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class FundNewsImpact(Base):
    """一个新闻事件对一只基金的可解释影响。"""

    __tablename__ = "fund_news_impacts"
    __table_args__ = (
        UniqueConstraint("event_id", "instrument_id", name="uq_fund_news_event_instrument"),
        Index("ix_fund_news_impacts_instrument_event", "instrument_id", "event_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("news_events.id"), index=True)
    instrument_id: Mapped[int] = mapped_column(ForeignKey("instruments.id"), index=True)
    relation_type: Mapped[str] = mapped_column(String(32), nullable=False)
    relevance_score: Mapped[float] = mapped_column(Float, nullable=False)
    exposure_ratio: Mapped[float] = mapped_column(Float, nullable=False)
    signed_score: Mapped[float] = mapped_column(Float, nullable=False)
    reason: Mapped[str] = mapped_column(String(500), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


__all__ = ["FundNewsImpact", "NewsEvent", "NewsEventItem"]
