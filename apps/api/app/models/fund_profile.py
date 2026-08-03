"""基金详细介绍缓存：按需抓取的基本概况与投资说明。"""

from datetime import date, datetime

from sqlalchemy import Date, DateTime, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class FundProfile(Base):
    __tablename__ = "fund_profiles"

    code: Mapped[str] = mapped_column(String(32), primary_key=True)
    short_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    full_name: Mapped[str | None] = mapped_column(String(300), nullable=True)
    inception_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    latest_scale: Mapped[str | None] = mapped_column(String(100), nullable=True)
    company: Mapped[str | None] = mapped_column(String(200), nullable=True)
    manager: Mapped[str | None] = mapped_column(String(200), nullable=True)
    custodian: Mapped[str | None] = mapped_column(String(200), nullable=True)
    fund_type: Mapped[str | None] = mapped_column(String(100), nullable=True)
    rating_agency: Mapped[str | None] = mapped_column(String(100), nullable=True)
    rating: Mapped[str | None] = mapped_column(String(100), nullable=True)
    investment_objective: Mapped[str | None] = mapped_column(Text, nullable=True)
    investment_strategy: Mapped[str | None] = mapped_column(Text, nullable=True)
    benchmark: Mapped[str | None] = mapped_column(Text, nullable=True)
    management_fee: Mapped[str | None] = mapped_column(String(100), nullable=True)
    custody_fee: Mapped[str | None] = mapped_column(String(100), nullable=True)
    source: Mapped[str | None] = mapped_column(String(50), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    fetched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
