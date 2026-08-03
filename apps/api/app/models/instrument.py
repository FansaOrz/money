"""基金标的模型：一只基金（或其他可投资标的）的基本信息。"""

import enum
from datetime import datetime

from sqlalchemy import DateTime, Enum, String, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


class InstrumentType(str, enum.Enum):
    """标的类型，MVP 聚焦基金，预留其他类型。"""

    FUND = "fund"
    STOCK = "stock"
    BOND = "bond"
    CASH = "cash"
    OTHER = "other"


class Instrument(Base):
    __tablename__ = "instruments"

    id: Mapped[int] = mapped_column(primary_key=True)
    # 基金代码，例如 110022，全局唯一
    code: Mapped[str] = mapped_column(String(32), unique=True, nullable=False, index=True)
    # 基金名称，例如“易方达消费行业股票”
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    type: Mapped[InstrumentType] = mapped_column(
        Enum(InstrumentType, native_enum=False, length=20),
        nullable=False,
        default=InstrumentType.FUND,
        server_default=InstrumentType.FUND.value,
    )
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="CNY")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    transactions: Mapped[list["Transaction"]] = relationship(  # noqa: F821
        back_populates="instrument"
    )
    positions: Mapped[list["Position"]] = relationship(  # noqa: F821
        back_populates="instrument"
    )
