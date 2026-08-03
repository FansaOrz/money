"""资讯条目模型：抓取到的新闻/快讯，按 content_hash 去重。"""

from datetime import datetime

from sqlalchemy import DateTime, Index, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class NewsItem(Base):
    __tablename__ = "news_items"
    __table_args__ = (
        Index("ix_news_items_published_at", "published_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    # 来源标识，例如 eastmoney_rss / cls_telegraph / akshare_stock_news
    source: Mapped[str] = mapped_column(String(64), nullable=False)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    url: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # 关联标的代码（基金/股票），逗号分隔；纯市场快讯为空
    related_codes: Mapped[str | None] = mapped_column(String(500), nullable=True)
    fetched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    # 内容指纹（source + title + url 的 sha256），全局唯一用于去重
    content_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
