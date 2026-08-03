"""A 股研究数据层模型（research repository）。

覆盖范围（与 AKShare 接口一一对应）：
- StockMaster：A 股代码/名称主表（ak.stock_info_a_code_name）
- IndexConstituent：沪深300/中证500 当前成分（ak.index_stock_cons_csindex）
- IndexMembershipEvent：指数成分调整事件（CSV 导入，可推导历史成分快照）
- StockDailyBar：日线行情元数据/断点（ak.stock_zh_a_daily，raw 实际存 Parquet 数据湖）
- StockFinancialIndicator：财务分析指标（ak.stock_financial_analysis_indicator）
- StockReportDisclosure：财报披露日程（ak.stock_report_disclosure）
- StockValuation：百度股市通估值（ak.stock_zh_valuation_baidu）
- StockNameHistory：历史名称/ST 变更（ak.stock_info_change_name，无精确日期时靠 sort_order）
- StockIndustry：股票行业归属（ak.stock_board_industry_cons_em 主源，THS 回退）

设计原则：
- 所有行带 source / available_at（数据可用时间），便于后续做 point-in-time 研究；
- 幂等 upsert，唯一约束见各表 __table_args__；
- 不伪造缺失数据：抓不到就留空，由 status 接口暴露 coverage。
"""

from datetime import date, datetime

from sqlalchemy import (
    Date,
    DateTime,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

PRICE = Numeric(18, 4)
PERCENT = Numeric(12, 4)


class StockMaster(Base):
    """A 股证券主表：code -> 当前名称（ak.stock_info_a_code_name）。"""

    __tablename__ = "stock_master"

    # 6 位代码，例如 600519 / 000001
    code: Mapped[str] = mapped_column(String(10), primary_key=True)
    name: Mapped[str] = mapped_column(String(50), nullable=False)
    # sh / sz / bj，由代码前缀推断
    exchange: Mapped[str | None] = mapped_column(String(4), nullable=True)
    source: Mapped[str] = mapped_column(String(30), nullable=False, default="akshare")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class IndexConstituent(Base):
    """指数当前成分快照（ak.index_stock_cons_csindex）。

    每次同步全量替换对应指数的成分，仅表达“当前”，历史成分走
    IndexMembershipEvent / 快照查询。
    """

    __tablename__ = "index_constituents"
    __table_args__ = (
        UniqueConstraint("index_code", "stock_code", name="uq_index_constituents_index_stock"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    # 指数代码，例如 000300（沪深300）/ 000905（中证500）
    index_code: Mapped[str] = mapped_column(String(10), nullable=False, index=True)
    index_name: Mapped[str | None] = mapped_column(String(50), nullable=True)
    stock_code: Mapped[str] = mapped_column(String(10), nullable=False, index=True)
    stock_name: Mapped[str | None] = mapped_column(String(50), nullable=True)
    # 中证指数公司公布的纳入日期，可能为空
    in_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    source: Mapped[str] = mapped_column(String(30), nullable=False, default="csindex")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class IndexMembershipEvent(Base):
    """指数成分调整事件（add/remove），由 CSV 导入。

    通过按日期回放事件可重建任意时点的成分快照。
    """

    __tablename__ = "index_membership_events"
    __table_args__ = (
        UniqueConstraint(
            "index_code",
            "stock_code",
            "effective_date",
            "event_type",
            name="uq_membership_events_natural",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    index_code: Mapped[str] = mapped_column(String(10), nullable=False, index=True)
    stock_code: Mapped[str] = mapped_column(String(10), nullable=False)
    stock_name: Mapped[str | None] = mapped_column(String(50), nullable=True)
    # add / remove
    event_type: Mapped[str] = mapped_column(String(10), nullable=False)
    effective_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    # 事件来源标识，例如 csv:文件名
    source: Mapped[str] = mapped_column(String(100), nullable=False, default="csv")
    # 数据可用时间（通常 >= effective_date 的公告时间；未知时取导入时间）
    available_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class StockDailyBar(Base):
    """日线行情断点/元数据表。

    raw（不复权）OHLCV 全量存 Parquet 数据湖（research_data_dir/daily/raw/<code>.parquet），
    本表记录每只股票的同步断点（first/last trade_date、行数、checksum），
    用于断点续传与 coverage 统计，不承载逐日行数据。
    """

    __tablename__ = "stock_daily_bars"

    code: Mapped[str] = mapped_column(String(10), primary_key=True)
    first_trade_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    last_trade_date: Mapped[date | None] = mapped_column(Date, nullable=True, index=True)
    rows: Mapped[int] = mapped_column(nullable=False, default=0)
    parquet_path: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # 最近一次成功同步时间（数据可用时间）
    available_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    source: Mapped[str] = mapped_column(String(30), nullable=False, default="sina")
    # 最近一次同步错误信息（网络失败等），成功时清空
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class StockFinancialIndicator(Base):
    """财务分析指标（ak.stock_financial_analysis_indicator，新浪）。

    按报告期（report_date）一行，保留原始字段为 JSON 文本，
    同时抽出常用字段（EPS/ROE）便于过滤。coverage 取决于新浪披露，
    早期报告期可能缺失，不补齐。
    """

    __tablename__ = "stock_financial_indicators"
    __table_args__ = (
        UniqueConstraint(
            "code", "report_date", name="uq_financial_indicators_code_report"
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(10), nullable=False, index=True)
    # 报告期，例如 2024-12-31
    report_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    eps: Mapped[float | None] = mapped_column(Numeric(18, 6), nullable=True)
    roe: Mapped[float | None] = mapped_column(PERCENT, nullable=True)
    # 原始接口返回的全部字段（JSON），列随新浪接口变化，不做强 schema
    payload: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    source: Mapped[str] = mapped_column(String(30), nullable=False, default="sina")
    # 数据可用时间：该指标进入本地库的时间（披露时间不可得时用它近似）
    available_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class StockReportDisclosure(Base):
    """财报披露日程（ak.stock_report_disclosure）。

    记录每个报告期的实际/预计披露日，配合 available_at 可回答
    “在某个历史交易日，哪些财报数据已经可见”（point-in-time）。
    """

    __tablename__ = "stock_report_disclosure"
    __table_args__ = (
        UniqueConstraint("code", "report_date", name="uq_report_disclosure_code_report"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(10), nullable=False, index=True)
    report_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    # 实际披露日（可能为空，表示尚未披露/数据源未给出）
    disclosure_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    # 预计披露日
    estimate_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    # 数据可用时间：实际披露日当日收盘后视为可用；无披露日则为空
    available_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    source: Mapped[str] = mapped_column(String(30), nullable=False, default="sina")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class StockValuation(Base):
    """估值指标（ak.stock_zh_valuation_baidu）。

    按 (code, trade_date, indicator) 存储：总市值/PE(TTM)/PB/PS(TTM)/股息率等。
    注意：百度接口只返回有限回看窗口，历史深度受数据源限制。
    """

    __tablename__ = "stock_valuations"
    __table_args__ = (
        UniqueConstraint(
            "code", "trade_date", "indicator", name="uq_valuations_code_date_indicator"
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(10), nullable=False, index=True)
    trade_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    # total_mv / pe_ttm / pb / ps_ttm / dividend_yield ...
    indicator: Mapped[str] = mapped_column(String(30), nullable=False)
    value: Mapped[float | None] = mapped_column(Numeric(24, 6), nullable=True)
    source: Mapped[str] = mapped_column(String(30), nullable=False, default="baidu")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class StockNameHistory(Base):
    """历史名称/ST 变更（ak.stock_info_change_name，新浪公司资料页）。

    当前接口只给曾用名序列（旧->新）不给精确日期：start_date 允许为空，
    区间顺序以 sort_order 表达（0 起，旧->新）；有显式日期的数据按
    (code, start_date, name) 幂等 upsert。
    """

    __tablename__ = "stock_name_history"
    __table_args__ = (
        UniqueConstraint(
            "code", "start_date", "name", name="uq_name_history_code_start_name"
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(10), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(50), nullable=False)
    # 区间起点；数据源不披露日期时为空（不伪造）
    start_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    end_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    # 区间顺序（旧->新，0 起）；start_date 为空时靠它表达先后
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # 该名称区间是否为 ST/*ST
    is_st: Mapped[bool] = mapped_column(nullable=False, default=False)
    # 变更原因（数据源原文，如「撤销退市风险警示」），可能为空
    change_reason: Mapped[str | None] = mapped_column(String(200), nullable=True)
    source: Mapped[str] = mapped_column(String(30), nullable=False, default="akshare")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class StockUniverseSnapshot(Base):
    """某个历史日期的指数成分快照（由事件回放/当前成分推导并物化）。

    membership 由事件流重算，可能因早期事件 CSV 缺失而不完整；
    消费方应结合 coverage 说明使用。
    """

    __tablename__ = "stock_universe_snapshots"
    __table_args__ = (
        UniqueConstraint(
            "index_code", "snapshot_date", "stock_code", name="uq_universe_snapshot_natural"
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    index_code: Mapped[str] = mapped_column(String(10), nullable=False, index=True)
    snapshot_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    stock_code: Mapped[str] = mapped_column(String(10), nullable=False)
    stock_name: Mapped[str | None] = mapped_column(String(50), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class StockIndustry(Base):
    """股票行业归属（ak.stock_board_industry_cons_em 东财为主源，THS 回退）。

    只表达“当前”归属（源接口不提供历史变更），全量替换式同步；
    按 (code, source) 幂等 upsert，不同源各自保留一行。
    """

    __tablename__ = "stock_industries"
    __table_args__ = (
        UniqueConstraint("code", "source", name="uq_stock_industries_code_source"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(10), nullable=False, index=True)
    name: Mapped[str | None] = mapped_column(String(50), nullable=True)
    # 行业分类口径：em（东方财富）/ ths（同花顺）
    source: Mapped[str] = mapped_column(String(30), nullable=False, default="em")
    industry_code: Mapped[str | None] = mapped_column(String(20), nullable=True)
    industry_name: Mapped[str] = mapped_column(String(50), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class StockSyncState(Base):
    """各研究数据任务的同步状态（断点/最近一次运行结果）。"""

    __tablename__ = "stock_sync_state"

    # master / universe / daily / financial / disclosure / valuation / name_history / industry
    task: Mapped[str] = mapped_column(String(30), primary_key=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="never_run")
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # 处理/成功/失败计数
    total: Mapped[int] = mapped_column(nullable=False, default=0)
    updated: Mapped[int] = mapped_column(nullable=False, default=0)
    failed: Mapped[int] = mapped_column(nullable=False, default=0)
    # 断点：日线同步处理到的最后一只股票代码
    last_code: Mapped[str | None] = mapped_column(String(10), nullable=True)
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)
