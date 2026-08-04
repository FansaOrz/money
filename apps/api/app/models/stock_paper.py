"""A 股规则策略前向模拟账本。

与基金 PaperAccount 分表，避免股票代码被强行映射为基金 Instrument：
- StockPaperAccount：绑定不可变 StrategyVersion 的两个月观察账户；
- StockPaperSignal：T 日收盘生成、T+1 起执行的目标权重快照；
- StockPaperPosition / StockPaperTrade：当前持仓与逐笔模拟成交；
- StockPaperRun / StockPaperNavDaily：每日幂等运行记录与净值/基准轨迹。

全部仅用于研究和模拟，不产生任何真实订单。
"""

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

MONEY = Numeric(18, 2)
PRICE = Numeric(18, 6)
QUANTITY = Numeric(24, 6)
WEIGHT = Numeric(12, 8)


class StockPaperAccount(Base):
    """一个股票策略版本对应的前向模拟账户。"""

    __tablename__ = "stock_paper_accounts"
    __table_args__ = (
        UniqueConstraint(
            "strategy_version_id", "name", name="uq_stock_paper_account_version_name"
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    strategy_version_id: Mapped[int] = mapped_column(
        ForeignKey("strategy_versions.id"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    initial_capital: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    cash: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    benchmark_nav: Mapped[Decimal] = mapped_column(
        PRICE, nullable=False, default=Decimal("1")
    )
    status: Mapped[str] = mapped_column(
        String(30), nullable=False, default="paper_testing"
    )
    trial_start: Mapped[date] = mapped_column(Date, nullable=False)
    trial_end: Mapped[date] = mapped_column(Date, nullable=False)
    candidate_codes: Mapped[list[str]] = mapped_column(
        JSON, nullable=False, default=list
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class StockPaperPosition(Base):
    """股票模拟账户当前持仓。"""

    __tablename__ = "stock_paper_positions"
    __table_args__ = (
        UniqueConstraint(
            "account_id", "stock_code", name="uq_stock_paper_position_account_code"
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(
        ForeignKey("stock_paper_accounts.id"), nullable=False, index=True
    )
    stock_code: Mapped[str] = mapped_column(String(10), nullable=False, index=True)
    shares: Mapped[Decimal] = mapped_column(QUANTITY, nullable=False)
    cost: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class StockPaperReceivable(Base):
    """除权日确认、派息日到账的现金股利应收款。"""

    __tablename__ = "stock_paper_receivables"
    __table_args__ = (
        UniqueConstraint(
            "account_id",
            "event_key",
            name="uq_stock_paper_receivable_account_event",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(
        ForeignKey("stock_paper_accounts.id"), nullable=False, index=True
    )
    stock_code: Mapped[str] = mapped_column(String(10), nullable=False, index=True)
    event_key: Mapped[str] = mapped_column(String(160), nullable=False)
    entitlement_date: Mapped[date] = mapped_column(Date, nullable=False)
    payment_date: Mapped[date | None] = mapped_column(Date)
    amount: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="receivable")
    source: Mapped[str] = mapped_column(String(500), nullable=False)
    paid_at: Mapped[date | None] = mapped_column(Date)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class StockPaperRun(Base):
    """某一真实行情日的前向模拟运行记录，按账户和数据日幂等。"""

    __tablename__ = "stock_paper_runs"
    __table_args__ = (
        UniqueConstraint(
            "account_id", "run_date", name="uq_stock_paper_run_account_date"
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(
        ForeignKey("stock_paper_accounts.id"), nullable=False, index=True
    )
    run_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    trading_day_index: Mapped[int] = mapped_column(Integer, nullable=False)
    signal_generated: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    rebalanced: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    trade_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    status: Mapped[str] = mapped_column(
        String(30), nullable=False, default="completed"
    )
    result: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    warnings: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class StockPaperSignal(Base):
    """T 日收盘固化的全量打分与目标权重，最早在 execute_on 执行。"""

    __tablename__ = "stock_paper_signals"
    __table_args__ = (
        UniqueConstraint(
            "account_id", "signal_date", name="uq_stock_paper_signal_account_date"
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(
        ForeignKey("stock_paper_accounts.id"), nullable=False, index=True
    )
    run_id: Mapped[int | None] = mapped_column(
        ForeignKey("stock_paper_runs.id"), nullable=True, index=True
    )
    signal_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    execute_on: Mapped[date | None] = mapped_column(Date, nullable=True)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="pending"
    )
    universe_count: Mapped[int] = mapped_column(Integer, nullable=False)
    selected_count: Mapped[int] = mapped_column(Integer, nullable=False)
    invested_weight: Mapped[Decimal] = mapped_column(WEIGHT, nullable=False)
    target_weights: Mapped[dict[str, float]] = mapped_column(
        JSON, nullable=False, default=dict
    )
    order_state: Mapped[dict[str, dict]] = mapped_column(
        JSON, nullable=False, default=dict
    )
    items: Mapped[list[dict]] = mapped_column(JSON, nullable=False, default=list)
    methodology: Mapped[str] = mapped_column(Text, nullable=False, default="")
    warnings: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    executed_at: Mapped[date | None] = mapped_column(Date, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class StockPaperTrade(Base):
    """一笔股票模拟成交。"""

    __tablename__ = "stock_paper_trades"

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(
        ForeignKey("stock_paper_accounts.id"), nullable=False, index=True
    )
    run_id: Mapped[int] = mapped_column(
        ForeignKey("stock_paper_runs.id"), nullable=False, index=True
    )
    signal_id: Mapped[int] = mapped_column(
        ForeignKey("stock_paper_signals.id"), nullable=False, index=True
    )
    stock_code: Mapped[str] = mapped_column(String(10), nullable=False, index=True)
    trade_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    side: Mapped[str] = mapped_column(String(8), nullable=False)
    shares: Mapped[Decimal] = mapped_column(QUANTITY, nullable=False)
    price: Mapped[Decimal] = mapped_column(PRICE, nullable=False)
    amount: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    fee: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    target_weight: Mapped[Decimal] = mapped_column(WEIGHT, nullable=False)
    reason: Mapped[str] = mapped_column(String(300), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class StockPaperNavDaily(Base):
    """股票模拟账户每日收盘估值及持仓快照。"""

    __tablename__ = "stock_paper_nav_daily"
    __table_args__ = (
        UniqueConstraint(
            "account_id", "nav_date", name="uq_stock_paper_nav_account_date"
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(
        ForeignKey("stock_paper_accounts.id"), nullable=False, index=True
    )
    run_id: Mapped[int] = mapped_column(
        ForeignKey("stock_paper_runs.id"), nullable=False
    )
    nav_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    cash: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    market_value: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    total_value: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    nav: Mapped[Decimal] = mapped_column(PRICE, nullable=False)
    daily_return: Mapped[Decimal | None] = mapped_column(WEIGHT, nullable=True)
    cumulative_return: Mapped[Decimal] = mapped_column(WEIGHT, nullable=False)
    benchmark_nav: Mapped[Decimal] = mapped_column(PRICE, nullable=False)
    benchmark_daily_return: Mapped[Decimal | None] = mapped_column(
        WEIGHT, nullable=True
    )
    fee_total: Mapped[Decimal] = mapped_column(
        MONEY, nullable=False, default=Decimal("0")
    )
    rebalanced: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    positions: Mapped[list[dict]] = mapped_column(JSON, nullable=False, default=list)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
