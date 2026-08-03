"""交易流水模型：一笔申购/赎回/分红等业务流水。

金额与份额统一使用 Decimal（NUMERIC），避免浮点误差。
"""

import enum
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import Date, DateTime, Enum, ForeignKey, Index, Numeric, String, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base

# 金额精度：18 位总位数，2 位小数
MONEY = Numeric(18, 2)
# 份额/净值精度：18 位总位数，4 位小数
QUANTITY = Numeric(18, 4)


class TransactionType(str, enum.Enum):
    """交易类型。"""

    BUY = "buy"              # 申购
    SELL = "sell"            # 赎回
    DIVIDEND = "dividend"    # 现金分红
    REINVEST = "reinvest"    # 红利再投资
    FEE = "fee"              # 费用
    TRANSFER_IN = "transfer_in"
    TRANSFER_OUT = "transfer_out"
    OTHER = "other"


class Transaction(Base):
    __tablename__ = "transactions"
    __table_args__ = (
        Index("ix_transactions_account_date", "account_id", "trade_date"),
        Index(
            "uq_transactions_order_fund_type",
            "external_order_hash",
            "instrument_id",
            "source_type",
            unique=True,
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    # 订单号仅保存 SHA-256，避免在数据库暴露完整流水号
    external_order_hash: Mapped[str | None] = mapped_column(
        String(64), nullable=True, index=True
    )
    source_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    account_id: Mapped[int] = mapped_column(
        ForeignKey("accounts.id"), nullable=False
    )
    instrument_id: Mapped[int] = mapped_column(
        ForeignKey("instruments.id"), nullable=False
    )
    # 来源导入批次（可空，手工录入时为 None）
    import_id: Mapped[int | None] = mapped_column(
        ForeignKey("imports.id"), nullable=True
    )
    type: Mapped[TransactionType] = mapped_column(
        Enum(TransactionType, native_enum=False, length=20), nullable=False
    )
    trade_date: Mapped[date] = mapped_column(Date, nullable=False)
    # 份额变动数量（买入为正、卖出为负由服务层约定）
    shares: Mapped[Decimal | None] = mapped_column(QUANTITY, nullable=True)
    # 成交净值（每份价格）
    nav: Mapped[Decimal | None] = mapped_column(QUANTITY, nullable=True)
    # 成交金额（含费）
    amount: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    # 手续费
    fee: Mapped[Decimal] = mapped_column(MONEY, nullable=False, default=Decimal("0"))
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="CNY")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    account: Mapped["Account"] = relationship(back_populates="transactions")  # noqa: F821
    instrument: Mapped["Instrument"] = relationship(back_populates="transactions")  # noqa: F821
    import_batch: Mapped["Import | None"] = relationship(back_populates="transactions")  # noqa: F821
