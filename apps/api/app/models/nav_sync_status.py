"""基金净值数据覆盖状态模型：记录每只基金的历史回填进度与断点。"""

from datetime import date, datetime

from sqlalchemy import Date, DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class NavSyncStatus(Base):
    """每只基金一行，记录净值历史覆盖范围、断点与最近一次失败原因。

    status 取值：
    - complete：已覆盖到目标起始日期（cutoff）
    - partial：已写入部分数据，但尚未推进到 cutoff，可用 next_end_date 断点续传
    - failed：最近一次同步失败（含 AKShare 回退也失败的情况）
    """

    __tablename__ = "nav_sync_status"

    id: Mapped[int] = mapped_column(primary_key=True)
    instrument_id: Mapped[int] = mapped_column(
        ForeignKey("instruments.id"), nullable=False, unique=True, index=True
    )
    # complete / partial / failed
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="partial")
    # 目标回填起点（5 年前）
    target_start_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    # 本地库中已有净值的最早日期
    earliest_nav_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    # 本地库中已有净值的最新日期
    latest_nav_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    # 断点续传游标：下一轮应从该日期之前继续回填（含该日期之前的数据）
    next_end_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    row_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # 最近一次使用的数据源：eastmoney / akshare / akshare_hk
    last_source: Mapped[str | None] = mapped_column(String(20), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
