"""基金季度重仓股与行业配置。"""

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import Date, DateTime, ForeignKey, Numeric, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class FundHolding(Base):
    __tablename__ = "fund_holdings"
    __table_args__ = (
        UniqueConstraint("instrument_id", "report_date", "stock_code", name="uq_fund_holding_report_stock"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    instrument_id: Mapped[int] = mapped_column(ForeignKey("instruments.id"), index=True)
    report_date: Mapped[date] = mapped_column(Date, index=True)
    rank: Mapped[int | None]
    stock_code: Mapped[str] = mapped_column(String(16), index=True)
    stock_name: Mapped[str] = mapped_column(String(100))
    weight: Mapped[Decimal] = mapped_column(Numeric(10, 4))
    shares: Mapped[Decimal | None] = mapped_column(Numeric(20, 4))
    market_value: Mapped[Decimal | None] = mapped_column(Numeric(20, 2))
    source: Mapped[str] = mapped_column(String(30), default="eastmoney")
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
