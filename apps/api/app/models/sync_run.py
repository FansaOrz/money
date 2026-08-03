"""同步运行记录模型：每个定时任务每次执行写一行，用于状态查询与排障。"""

from datetime import date, datetime

from sqlalchemy import Date, DateTime, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class SyncRun(Base):
    """一次同步任务的执行记录。

    status 取值：
    - running：任务已开始，尚未结束
    - success：执行完成且无失败项
    - partial：执行完成但有部分失败（updated>0 且 failed>0）
    - failed：任务整体异常退出或全部失败，error 记录异常信息
    - paused：任务被显式暂停/跳过（人为标记，不算成功）
    """

    __tablename__ = "sync_runs"
    __table_args__ = (
        Index("ix_sync_runs_job_started", "job_name", "started_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    # 任务标识，例如 fund_nav / indices / us_indices / news / holdings / paper / stock_daily
    job_name: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    # running / success / partial / failed / paused
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="running")
    # 开始/结束时间（北京时间，aware）
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # 处理对象总数（基金数 / 指数数 / 抓取条数等）
    total: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # 成功更新数量
    updated: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # 失败数量
    failed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # 本次同步覆盖的数据日期（如最新净值日期），无则留空
    data_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    # 整体异常时的错误信息
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
