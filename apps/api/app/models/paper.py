"""模拟交易（Paper Trading）模型：虚拟账户、信号快照、虚拟成交与每日估值。

全部为模拟交易记录，不涉及任何真实下单：
- StrategyVersion：策略配置版本（初始资金、调仓间隔、费用率、入选数量）；
- BacktestRun：每日 run_paper_cycle 的一次执行记录，保证幂等；
- PaperAccount / PaperPosition / PaperTrade：虚拟账户、持仓与成交；
- SignalSnapshot：调仓日固化的全候选信号快照；
- PaperNavDaily / PaperHoldingDaily：每日账户净值与持仓估值。
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
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base

MONEY = Numeric(18, 2)
QUANTITY = Numeric(18, 6)
WEIGHT = Numeric(10, 6)

# 默认策略参数：初始资金 100 万元、每 20 个交易日调仓、双边简化费用 0.1%
DEFAULT_INITIAL_CAPITAL = Decimal("1000000")
DEFAULT_REBALANCE_INTERVAL = 20
DEFAULT_FEE_RATE = Decimal("0.001")
DEFAULT_TOP_N = 10


class StrategyVersion(Base):
    """策略配置版本：参数变化时新增版本，历史运行记录可追溯到具体版本。"""

    __tablename__ = "strategy_versions"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    initial_capital: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    # 每多少个交易日调仓一次
    rebalance_interval: Mapped[int] = mapped_column(Integer, nullable=False)
    # 双边简化费用率（买入、卖出各收一次，小数，如 0.001 = 0.1%）
    fee_rate: Mapped[Decimal] = mapped_column(WEIGHT, nullable=False)
    top_n: Mapped[int] = mapped_column(Integer, nullable=False)
    params: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="research")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    accounts: Mapped[list["PaperAccount"]] = relationship(back_populates="strategy_version")


class PaperAccount(Base):
    """模拟账户：每个策略版本一个默认账户（单账户 MVP）。"""

    __tablename__ = "paper_accounts"
    __table_args__ = (
        UniqueConstraint("strategy_version_id", "name", name="uq_paper_accounts_version_name"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    strategy_version_id: Mapped[int] = mapped_column(
        ForeignKey("strategy_versions.id"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(100), nullable=False, default="默认模拟账户")
    initial_capital: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    cash: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="CNY")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    strategy_version: Mapped["StrategyVersion"] = relationship(back_populates="accounts")
    positions: Mapped[list["PaperPosition"]] = relationship(back_populates="account")
    trades: Mapped[list["PaperTrade"]] = relationship(back_populates="account")


class PaperPosition(Base):
    """模拟账户的当前持仓（份额口径，估值落在 PaperHoldingDaily）。"""

    __tablename__ = "paper_positions"
    __table_args__ = (
        UniqueConstraint("account_id", "instrument_id", name="uq_paper_positions_account_instrument"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(
        ForeignKey("paper_accounts.id"), nullable=False, index=True
    )
    instrument_id: Mapped[int] = mapped_column(
        ForeignKey("instruments.id"), nullable=False, index=True
    )
    shares: Mapped[Decimal] = mapped_column(QUANTITY, nullable=False)
    cost: Mapped[Decimal] = mapped_column(MONEY, nullable=False, default=Decimal("0"))

    account: Mapped["PaperAccount"] = relationship(back_populates="positions")


class BacktestRun(Base):
    """一次 run_paper_cycle 的执行记录：同日重跑直接复用（幂等）。"""

    __tablename__ = "backtest_runs"
    __table_args__ = (
        UniqueConstraint("account_id", "run_date", name="uq_backtest_runs_account_date"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(
        ForeignKey("paper_accounts.id"), nullable=False, index=True
    )
    run_date: Mapped[date] = mapped_column(Date, nullable=False)
    # 自账户建立以来经过的净值交易日数量（首个调仓日为 1）
    trading_day_index: Mapped[int] = mapped_column(Integer, nullable=False)
    rebalanced: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    trade_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    run_type: Mapped[str] = mapped_column(String(30), nullable=False, default="paper_daily")
    params: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    result: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="completed")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class PaperTrade(Base):
    """一笔虚拟成交（调仓日产生，仅模拟，不真实下单）。"""

    __tablename__ = "paper_trades"

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(
        ForeignKey("paper_accounts.id"), nullable=False, index=True
    )
    run_id: Mapped[int] = mapped_column(
        ForeignKey("backtest_runs.id"), nullable=False, index=True
    )
    instrument_id: Mapped[int] = mapped_column(
        ForeignKey("instruments.id"), nullable=False, index=True
    )
    trade_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    side: Mapped[str] = mapped_column(String(8), nullable=False)  # buy / sell
    shares: Mapped[Decimal] = mapped_column(QUANTITY, nullable=False)
    price: Mapped[Decimal] = mapped_column(QUANTITY, nullable=False)
    amount: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    fee: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    target_weight: Mapped[Decimal | None] = mapped_column(WEIGHT, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    account: Mapped["PaperAccount"] = relationship(back_populates="trades")


class SignalSnapshot(Base):
    """调仓日固化的全候选信号快照（screener 结果，items 为 JSON）。"""

    __tablename__ = "signal_snapshots"
    __table_args__ = (
        UniqueConstraint("account_id", "signal_date", name="uq_signal_snapshots_account_date"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(
        ForeignKey("paper_accounts.id"), nullable=False, index=True
    )
    run_id: Mapped[int | None] = mapped_column(
        ForeignKey("backtest_runs.id"), nullable=True, index=True
    )
    signal_date: Mapped[date] = mapped_column(Date, nullable=False)
    as_of: Mapped[str | None] = mapped_column(String(10), nullable=True)
    methodology: Mapped[str] = mapped_column(String(2000), nullable=False, default="")
    candidate_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    excluded_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    observe_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    selected_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    items: Mapped[list[dict]] = mapped_column(JSON, nullable=False, default=list)
    warnings: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class PaperNavDaily(Base):
    """模拟账户每日净值（含现金、市值、费用与基准）。"""

    __tablename__ = "paper_nav_daily"
    __table_args__ = (
        UniqueConstraint("account_id", "nav_date", name="uq_paper_nav_daily_account_date"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(
        ForeignKey("paper_accounts.id"), nullable=False, index=True
    )
    run_id: Mapped[int | None] = mapped_column(
        ForeignKey("backtest_runs.id"), nullable=True
    )
    nav_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    cash: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    market_value: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    total_value: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    # 单位净值：total_value / initial_capital，起点 1.0
    nav: Mapped[Decimal] = mapped_column(QUANTITY, nullable=False)
    daily_return: Mapped[Decimal | None] = mapped_column(WEIGHT, nullable=True)
    cumulative_return: Mapped[Decimal | None] = mapped_column(WEIGHT, nullable=True)
    # 候选池等权基准（起点 1.0，按共同交易日逐日等权收益累计）
    benchmark_nav: Mapped[Decimal | None] = mapped_column(QUANTITY, nullable=True)
    benchmark_daily_return: Mapped[Decimal | None] = mapped_column(WEIGHT, nullable=True)
    # 当日调仓产生的费用合计
    fee_total: Mapped[Decimal] = mapped_column(MONEY, nullable=False, default=Decimal("0"))
    rebalanced: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class PaperHoldingDaily(Base):
    """模拟账户每日持仓估值快照（份额 × 当日净值）。"""

    __tablename__ = "paper_holding_daily"
    __table_args__ = (
        UniqueConstraint(
            "account_id", "holding_date", "instrument_id",
            name="uq_paper_holding_daily_account_date_instrument",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(
        ForeignKey("paper_accounts.id"), nullable=False, index=True
    )
    instrument_id: Mapped[int] = mapped_column(
        ForeignKey("instruments.id"), nullable=False, index=True
    )
    holding_date: Mapped[date] = mapped_column(Date, nullable=False)
    shares: Mapped[Decimal] = mapped_column(QUANTITY, nullable=False)
    nav: Mapped[Decimal] = mapped_column(QUANTITY, nullable=False)
    market_value: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    weight: Mapped[Decimal] = mapped_column(WEIGHT, nullable=False)
