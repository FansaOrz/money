"""主要市场指数模型：指数元数据与日线行情（OHLC）。

MarketIndex 记录跟踪的指数清单（上证、沪深300、恒生、恒生科技、标普500、纳指）；
IndexQuote 按 (index_id, trade_date) 幂等存储日线 OHLC/成交量/涨跌幅。
"""

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Date,
    DateTime,
    ForeignKey,
    Numeric,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base

PRICE = Numeric(18, 4)
PERCENT = Numeric(10, 4)


class MarketIndex(Base):
    __tablename__ = "market_indices"

    id: Mapped[int] = mapped_column(primary_key=True)
    # 内部统一代码，例如 SH000001 / CSI300 / HSI / HSTECH / SPX / IXIC
    code: Mapped[str] = mapped_column(String(20), unique=True, nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    name_en: Mapped[str | None] = mapped_column(String(100), nullable=True)
    # 市场分类：cn / hk / us
    market: Mapped[str] = mapped_column(String(10), nullable=False)
    currency: Mapped[str] = mapped_column(String(10), nullable=False, default="CNY")
    # 数据源代码，例如 sh000001 / HSI / .INX
    source_symbol: Mapped[str] = mapped_column(String(30), nullable=False)
    source: Mapped[str] = mapped_column(String(30), nullable=False, default="sina")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    quotes: Mapped[list["IndexQuote"]] = relationship(
        back_populates="index", cascade="all, delete-orphan"
    )


class IndexQuote(Base):
    __tablename__ = "index_quotes"
    __table_args__ = (
        UniqueConstraint("index_id", "trade_date", name="uq_index_quotes_index_date"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    index_id: Mapped[int] = mapped_column(
        ForeignKey("market_indices.id"), nullable=False, index=True
    )
    trade_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    open: Mapped[Decimal | None] = mapped_column(PRICE, nullable=True)
    high: Mapped[Decimal | None] = mapped_column(PRICE, nullable=True)
    low: Mapped[Decimal | None] = mapped_column(PRICE, nullable=True)
    close: Mapped[Decimal] = mapped_column(PRICE, nullable=False)
    volume: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    # 日涨跌幅（%），由相邻两个交易日收盘价计算
    change_pct: Mapped[Decimal | None] = mapped_column(PERCENT, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    index: Mapped[MarketIndex] = relationship(back_populates="quotes")
