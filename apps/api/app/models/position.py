"""持仓模型：某账户在某基金上的当前持仓汇总。

由交易流水滚动计算而来（或解析对账单直接得出）。
"""

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import Date, DateTime, ForeignKey, Numeric, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base

# 与 transaction.py 保持一致的精度定义
MONEY = Numeric(18, 2)
QUANTITY = Numeric(18, 4)


class Position(Base):
    __tablename__ = "positions"
    __table_args__ = (
        UniqueConstraint("account_id", "instrument_id", name="uq_positions_account_instrument"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(
        ForeignKey("accounts.id"), nullable=False
    )
    instrument_id: Mapped[int] = mapped_column(
        ForeignKey("instruments.id"), nullable=False
    )
    # 当前持有份额
    shares: Mapped[Decimal] = mapped_column(QUANTITY, nullable=False)
    # 持仓成本（总投入，用于计算收益率）
    cost: Mapped[Decimal] = mapped_column(MONEY, nullable=False, default=Decimal("0"))
    # 最新市值（无行情时可先等于成本）
    market_value: Mapped[Decimal | None] = mapped_column(MONEY, nullable=True)
    latest_nav: Mapped[Decimal | None] = mapped_column(QUANTITY, nullable=True)
    nav_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    account: Mapped["Account"] = relationship(back_populates="positions")  # noqa: F821
    instrument: Mapped["Instrument"] = relationship(back_populates="positions")  # noqa: F821
