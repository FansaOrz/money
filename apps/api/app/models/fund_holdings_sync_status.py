"""基金季度重仓/行业披露回填状态模型：按基金 × 年度记录同步进度与断点。

与 NavSyncStatus（每基金一行）不同，披露回填按年度推进（历史多年度由多次
--year 运行完成），因此状态行以 (instrument_id, year) 唯一：

status 取值：
- complete：该年度重仓与行业披露均已抓取并写入；
- partial：重仓或行业两者之一无数据/未成功（行业接口缺数据属常见情况，
  仅标记 partial，便于后续重试）；
- failed：抓取过程抛异常，last_error 记录原因，下轮优先重试。
"""

from datetime import datetime

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class FundHoldingsSyncStatus(Base):
    __tablename__ = "fund_holdings_sync_status"
    __table_args__ = (
        UniqueConstraint("instrument_id", "year", name="uq_fund_holdings_sync_status_year"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    instrument_id: Mapped[int] = mapped_column(
        ForeignKey("instruments.id"), nullable=False, index=True
    )
    year: Mapped[int] = mapped_column(Integer, nullable=False)
    # complete / partial / failed
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="partial")
    holding_rows: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    industry_rows: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    fetched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
