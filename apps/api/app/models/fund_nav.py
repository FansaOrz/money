"""基金每日净值模型。"""

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import Date, DateTime, ForeignKey, Numeric, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

QUANTITY = Numeric(18, 6)


class FundNav(Base):
    __tablename__ = "fund_navs"
    __table_args__ = (
        UniqueConstraint("instrument_id", "nav_date", name="uq_fund_navs_instrument_date"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    instrument_id: Mapped[int] = mapped_column(ForeignKey("instruments.id"), nullable=False, index=True)
    nav_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    unit_nav: Mapped[Decimal] = mapped_column(QUANTITY, nullable=False)
    accumulated_nav: Mapped[Decimal | None] = mapped_column(QUANTITY, nullable=True)
    daily_growth_rate: Mapped[Decimal | None] = mapped_column(Numeric(10, 4), nullable=True)
    source: Mapped[str] = mapped_column(default="eastmoney", nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
