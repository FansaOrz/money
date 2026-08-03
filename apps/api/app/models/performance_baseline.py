"""支付宝收益基准，用于对齐平台累计和年度收益口径。"""

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import Date, DateTime, Numeric, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

MONEY = Numeric(18, 2)


class PerformanceBaseline(Base):
    __tablename__ = "performance_baselines"

    id: Mapped[int] = mapped_column(primary_key=True)
    as_of_date: Mapped[date] = mapped_column(Date, nullable=False)
    market_value: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    cumulative_profit: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    current_year_profit: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    previous_year_profit: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
