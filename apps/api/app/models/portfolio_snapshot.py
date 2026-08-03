"""组合快照模型：每日（或按需要）记录组合整体价值，用于收益曲线。"""

from datetime import date
from decimal import Decimal

from sqlalchemy import Date, Numeric, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

MONEY = Numeric(18, 2)


class PortfolioSnapshot(Base):
    __tablename__ = "portfolio_snapshots"
    __table_args__ = (
        UniqueConstraint("snapshot_date", "account_id", name="uq_snapshots_date_account"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    snapshot_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    # 可空：None 表示全账户整体快照
    account_id: Mapped[int | None] = mapped_column(nullable=True)
    total_cost: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    total_market_value: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    # 当日收益（市值 - 成本），冗余便于查询
    total_profit: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="CNY")
