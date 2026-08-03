"""全市场基金净值写入研究仓库（DuckDB warehouse）的同步状态模型。

与 ``nav_sync_status`` 的区别：
- ``nav_sync_status`` 以 ``instruments.id`` 为键，只跟踪持仓基金的 SQLite ``fund_navs`` 回填；
- 本表以基金代码为键，跟踪 ``fund_catalog`` 全市场 active 基金直接写入
  研究仓库（``fund_nav`` 数据集）的进度，不要求存在 ``Instrument`` 记录。

status 取值：
- complete：目标窗口数据已全部写入仓库（含该基金已无更早净值的情况）
- failed：最近一次同步全部来源失败
"""

from datetime import date, datetime

from sqlalchemy import Date, DateTime, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class FundWarehouseSyncState(Base):
    """每只目录基金一行，记录研究仓库净值覆盖范围与最近一次失败原因。"""

    __tablename__ = "fund_warehouse_sync_state"

    id: Mapped[int] = mapped_column(primary_key=True)
    # fund_catalog.code；目录基金不要求存在 Instrument
    code: Mapped[str] = mapped_column(String(32), unique=True, nullable=False, index=True)
    # complete / failed
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="failed")
    # 目标回填起点（默认 5 年前）
    target_start_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    # 已写入仓库的净值最早/最新日期
    earliest_nav_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    latest_nav_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    row_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # 最近一次使用的数据源：eastmoney_fast / eastmoney / akshare / akshare_hk
    last_source: Mapped[str | None] = mapped_column(String(20), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
