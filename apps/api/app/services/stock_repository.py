"""A股多因子研究的数据仓储：数据契约 + 现有数据层适配器 + 动态装载。

数据契约（纯 Python 数据类，与任何 ORM/数据源解耦，服务层只依赖本模块）：
- StockBar：日线行情（OHLC、成交额、停牌、原始涨跌幅），
  收盘价一律为 raw（不复权）价，成交价与涨跌停判定都在 raw 价上进行；
- MarketBars：研究面板数据载体 —— research_bars（研究因子/打分用，
  优先前复权 qfq 序列以正确处理分红送转；缺失时回退 raw）与
  exec_bars（执行口径 raw：成交、流动性、停牌、涨跌停判定）分离；
- Fundamentals：PIT 财务快照（available_at 为信息可获得的最早日期，
  打分日 T 只可使用 available_at ≤ T 的数据）；
- StockInfo：股票元信息（名称、行业、上市日期）；
- NamePeriod：历史名称/ST 区间（按 as_of 判定历史 ST 状态）；
- TradeCalendar：交易日历与下一交易日查询。

仓储装载（load_repository）优先级：
1. 调用方显式注入（路由层 Depends / 测试注入 mock）；
2. 已注册的工厂（register_repository_factory，供未来模块挂钩）；
3. 约定模块探测 app.services.stock_repository.get_repository(session)
   （self-probe：未来若要整体替换仓储实现，在同名模块追加该函数即可，
   不必改动本适配器）；
4. 内置 SQL 适配器 SqlStockRepository（默认路径，永远可用）。

SqlStockRepository 的数据来源（全部为已存在的数据层，动态降级）：
- 股票清单/名称：stock_master（ORM）；
- 日线行情：Parquet 数据湖 daily/raw（research_data_dir），缺失时回退
  DuckDB 研究仓库（app.research，settings.research_db，存在才连接，
  read_only）；
- PIT 财务：stock_financial_indicators（roe/eps/payload 解析）+
  stock_report_disclosure（实际披露日 15:00 为 available_at，缺失回退
  财务行自身 available_at）+ stock_valuations（pe_ttm/pb → EP/BP）；
- 行业：stock_master 无行业字段时探测 StockIndustry 等候选模型，
  全部缺失按「未知」行业处理（全行业组内横截面，自动降级）；
- 交易日历：行情日期的并集；
- 指数基准：app.models 的 MarketIndex/IndexQuote（探测字段名）。

仅读不写；任何单源缺失只影响对应因子字段，不阻断整体流程。
"""

from __future__ import annotations

import importlib
import hashlib
import json
import logging
import math
from bisect import bisect_right
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Protocol

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 数据契约
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StockBar:
    """一根日线行情（raw 价口径，不复权）。"""

    code: str
    trade_date: date
    open: float | None
    high: float | None
    low: float | None
    close: float  # raw 收盘价（成交价口径）
    volume: float | None = None  # 成交量（股）
    amount: float | None = None  # 成交额（元）
    suspended: bool = False  # 停牌（当日无成交，close 通常为前收盘）
    raw_return: float | None = None  # 当日原始涨跌幅（优先用于涨跌停判定）
    up_limit: float | None = None  # 当日真实涨停价（执行口径）
    down_limit: float | None = None  # 当日真实跌停价（执行口径）


@dataclass(frozen=True)
class Fundamentals:
    """PIT 财务/估值快照：同一 code 可有多条（按 available_at 取最新）。

    - roe：净资产收益率（小数）；gross_margin：销售毛利率（小数）；
    - ocf_to_profit：经营现金流 / 净利润（倍数）；
    - debt_ratio：资产负债率（小数）；
    - ep：盈利收益率 E/P（小数）；bp：账面市值比 B/P（小数）。
    """

    code: str
    available_at: date  # 信息可获得日（PIT 过滤依据）
    period: date | None = None  # 报告期（展示用）
    valuation_date: date | None = None  # 估值实际交易日（新鲜度门禁）
    roe: float | None = None
    gross_margin: float | None = None
    ocf_to_profit: float | None = None
    cash_conversion_assets: float | None = None
    profit_classification: str | None = None
    debt_ratio: float | None = None
    ep: float | None = None
    bp: float | None = None
    market_cap: float | None = None  # 信号日总市值（元）
    float_market_cap: float | None = None
    roa: float | None = None
    net_margin: float | None = None
    revenue: float | None = None
    net_income: float | None = None
    operating_cash_flow: float | None = None
    capital_expenditure: float | None = None
    free_cash_flow: float | None = None
    free_cash_flow_definition: str | None = None
    total_assets: float | None = None
    total_equity: float | None = None
    dividend_yield: float | None = None
    dividend_yield_status: str | None = None
    dividend_yield_reason: str | None = None
    dividend_event_count: int = 0
    dividend_source_hashes: tuple[str, ...] = ()
    sales_yield: float | None = None
    company_type: str | None = None
    bank_net_interest_margin: float | None = None
    bank_npl_ratio: float | None = None
    bank_provision_coverage_ratio: float | None = None
    bank_capital_adequacy_ratio: float | None = None
    bank_loan_deposit_ratio: float | None = None
    broker_proprietary_risk_ratio: float | None = None
    broker_leverage_ratio: float | None = None
    broker_net_capital_ratio: float | None = None
    insurance_solvency_ratio: float | None = None
    insurance_combined_ratio: float | None = None
    insurance_reserve_coverage_ratio: float | None = None
    sector_metric_sources: tuple[str, ...] = ()
    formal_factor_usable: bool = True
    financial_quality_reasons: tuple[str, ...] = ()
    unit_policy: str | None = None
    flow_basis: str | None = None
    audit_opinion: str | None = None
    correction_status: str | None = None
    ttm_component_periods: tuple[date, ...] = ()


@dataclass(frozen=True)
class StockInfo:
    """股票元信息。"""

    code: str
    name: str
    industry: str = "未知"  # 行业分类；数据缺失时为「未知」（自动降级）
    list_date: date | None = None  # 上市日期（缺失时按行情首日近似）


@dataclass(frozen=True)
class TradeCalendar:
    """交易日历（升序、去重）。"""

    days: tuple[date, ...]

    def next_trade_day(self, day: date) -> date | None:
        """严格晚于 day 的第一个交易日；无则 None。"""
        for candidate in self.days:
            if candidate > day:
                return candidate
        return None

    def prev_trade_day(self, day: date) -> date | None:
        """严格早于 day 的最后一个交易日；无则 None。"""
        result: date | None = None
        for candidate in self.days:
            if candidate >= day:
                break
            result = candidate
        return result

    def is_trade_day(self, day: date) -> bool:
        return day in set(self.days)


@dataclass(frozen=True)
class NamePeriod:
    """一段历史名称区间（含 ST 标记），start_date ≤ as_of ≤ end_date 生效。

    end_date 为 None 表示该名称沿用至今。用于历史 ST 判定：
    打分日 T 的 ST 状态必须按 T 当天的名称（而非当前名称）判定。
    """

    code: str
    name: str
    start_date: date
    end_date: date | None = None
    is_st: bool = False


@dataclass(frozen=True)
class MarketBars:
    """一只股票的双口径日线面板：研究口径与执行口径分离。

    - research_bars：研究因子/打分/估值分位用。优先前复权（qfq）序列，
      分红送转不会制造虚假涨跌；qfq 缺失时回退 raw（与旧行为一致）。
      该序列只用于收益率类研究计算，绝不用于成交价。
    - exec_bars：执行口径 raw 序列。成交价、成交额流动性、停牌判定、
      涨跌停判定一律使用该序列。
    """

    research_bars: tuple[StockBar, ...]
    exec_bars: tuple[StockBar, ...]


@dataclass(frozen=True)
class UniverseMembership:
    """一个研究日期的指数成分并集及其实际快照日期。"""

    as_of: date
    members: frozenset[str]
    snapshot_dates: dict[str, date]
    missing_indices: tuple[str, ...] = ()


@dataclass(frozen=True)
class BenchmarkSeries:
    """带数据血缘和完整性摘要的指数净值序列。"""

    code: str
    name: str
    return_kind: str
    points: tuple[tuple[date, float], ...]
    source: str
    source_files: tuple[str, ...]
    source_hashes: tuple[str, ...]


@dataclass(frozen=True)
class IndexWeightSnapshot:
    """某指数在指定历史时点可用的完整官方成分权重截面。"""

    index_code: str
    as_of: date
    snapshot_date: date
    weights: tuple[tuple[str, float], ...]
    weight_sum_percent: float
    source_files: tuple[str, ...]
    source_hashes: tuple[str, ...]


@dataclass(frozen=True)
class CombinedIndexWeights:
    """沪深300与中证500固定 50/50 袖套合成权重。"""

    as_of: date
    method: str
    weights: tuple[tuple[str, float], ...]
    component_snapshots: tuple[IndexWeightSnapshot, ...]


@dataclass(frozen=True)
class CorporateAction:
    """影响原始价持仓口径的公司行为事件。

    share_ratio 为每持有一股新增的股份数；cash_per_share 为每股实际现金；
    terminal_price 仅用于退市/现金收购等终止持仓事件。event_key 把除权日
    的应收确认与派息日的现金到账关联起来；配股事件使用 subscription_*。
    """

    code: str
    action_date: date
    kind: str  # cash_dividend / stock_dividend / terminal
    cash_per_share: float = 0.0
    share_ratio: float = 0.0
    terminal_price: float | None = None
    event_key: str | None = None
    payment_date: date | None = None
    subscription_ratio: float = 0.0
    subscription_price: float | None = None
    successor_code: str | None = None
    record_date: date | None = None
    terminal_type: str | None = None
    consideration_status: str = "unknown"
    restricted_valuation_per_share: float = 0.0
    review_status: str = "unreviewed"
    rights_tradable: bool = False
    right_market_price: float | None = None
    subscription_deadline: date | None = None
    successor_listing_date: date | None = None
    cash_compensation_per_fraction: float | None = None
    fractional_handling: str = "cash_if_official_else_restricted"
    source_hash: str | None = None
    revision: int = 1
    source: str = "tushare"


# ---------------------------------------------------------------------------
# A股交易规则链：板块识别 / 涨跌停幅度 / 一字板 / ST（纯函数，服务层共用）
# ---------------------------------------------------------------------------

LIMIT_MAIN_BOARD = 0.10  # 主板（60/00）：±10%
LIMIT_GROWTH_BOARD = 0.20  # 创业板（30）/ 科创板（68）：±20%
LIMIT_BSE = 0.30  # 北交所（4/8/92 开头）：±30%
LIMIT_ST = 0.05  # ST/*ST（沪深，不含北交所）：±5%
# 一字板容忍带：|move| ≥ limit × (1 - ε) 视为一字（买/卖全队列被堵）
ONE_WORD_EPS = 0.002


def is_st_name(name: str) -> bool:
    """名称含 ST（含 *ST、退 等退市风险标识）。"""
    upper = name.upper().replace(" ", "")
    return "ST" in upper or "退" in name


def board_of(code: str) -> str:
    """按代码前缀识别板块：bse（北交所）/ star / chinext / main。

    北交所代码段：4xxxxx、8xxxxx、920xxx（2024 起新号段，代码以
    「92」开头，现行启用的是 920xxx 六位段）。
    """
    if code.startswith(("4", "8", "92")):
        return "bse"
    if code.startswith("68"):
        return "star"
    if code.startswith("30"):
        return "chinext"
    return "main"


def price_limit_for(code: str, st: bool = False) -> float:
    """板块涨跌停幅度（小数）。ST 5% 不适用于北交所（北交所无 ST 5% 档）。"""
    board = board_of(code)
    if board == "bse":
        return LIMIT_BSE
    if st:
        return LIMIT_ST
    if board in ("star", "chinext"):
        return LIMIT_GROWTH_BOARD
    return LIMIT_MAIN_BOARD


def one_word_limit(bar: StockBar, move: float, limit: float) -> bool:
    """一字涨跌停：日内振幅≈0 且 |涨跌幅| 在法定板幅 limit 的 ±ε 带内。

    limit 为板块法定幅度（10%/20%/30%/5%，未经触发容差缩放）。
    一字板当日买卖队列全堵，任何方向都不可成交（区别于普通触板只堵
    反向：涨停不可买、跌停不可卖）。high/low 缺失时无法确认一字，
    保守按非一字处理（仅按方向阻塞）。
    """
    if bar.high is None or bar.low is None or bar.high <= 0 or bar.low <= 0:
        return False
    if bar.high - bar.low > 1e-9:
        return False
    return abs(abs(move) - limit) <= limit * ONE_WORD_EPS


def st_status_as_of(
    current_name: str,
    periods: list[NamePeriod] | tuple[NamePeriod, ...] | None,
    as_of: date,
) -> bool:
    """as_of 当日的 ST 状态：名称历史优先（区间命中以其 is_st 为准），
    无历史记录覆盖该日时回退当前名称判定。"""
    if periods:
        covering = [
            period
            for period in periods
            if period.start_date <= as_of
            and (period.end_date is None or period.end_date >= as_of)
        ]
        if covering:
            latest = max(covering, key=lambda period: period.start_date)
            return latest.is_st
    return is_st_name(current_name)


# ---------------------------------------------------------------------------
# 仓储协议（duck-typing：未来实现只需提供同名方法，无需继承本 Protocol）
# ---------------------------------------------------------------------------


class StockRepository(Protocol):
    """股票研究数据仓储协议。"""

    def list_stocks(self, codes: list[str] | None = None) -> list[StockInfo]:
        """股票清单；codes 为 None 时返回全市场。"""
        ...

    def trade_calendar(self, start: date | None, end: date | None) -> TradeCalendar:
        """区间内交易日（升序）；缺省返回全部已知交易日。"""
        ...

    def daily_bars(
        self,
        codes: list[str] | None = None,
        start: date | None = None,
        end: date | None = None,
    ) -> list[StockBar]:
        """日线行情（任意顺序，服务层自行排序）。"""
        ...

    def fundamentals(
        self,
        codes: list[str] | None = None,
        as_of: date | None = None,
    ) -> list[Fundamentals]:
        """PIT 财务快照；实现方必须按 available_at ≤ as_of 过滤（若传）。"""
        ...

    def index_bars(
        self,
        index_code: str,
        start: date | None = None,
        end: date | None = None,
    ) -> list[tuple[date, float]]:
        """指数收盘点位序列（升序），用作指数基准；不支持时返回 []。"""
        ...

    def benchmark_series(
        self,
        index_code: str,
        start: date | None = None,
        end: date | None = None,
    ) -> BenchmarkSeries | None:
        """返回受治理的官方基准序列；不可验证时返回 None。"""
        ...

    def index_weight_snapshot(
        self, index_code: str, as_of: date
    ) -> IndexWeightSnapshot:
        """查询不晚于 as_of 的最新完整官方权重截面。"""
        ...

    def combined_csi800_weights(self, as_of: date) -> CombinedIndexWeights:
        """以固定 50/50 袖套法合成 300+500 权重。"""
        ...

    # ---- 可选扩展（duck-typed，缺失时服务层自动降级）----------------------
    # def market_bars(self, codes, start=None, end=None) -> dict[str, MarketBars]:
    #     研究(qfq)/执行(raw) 双口径面板；缺失时引擎退化为 raw 单口径。
    # def name_histories(self, codes) -> dict[str, list[NamePeriod]]:
    #     历史名称/ST 区间；缺失时按当前名称判定 ST。
    # def valuation_snapshots(self, codes, as_of_dates) -> list[Fundamentals]:
    #     指定信号日的 PIT 估值快照；每个日期只取 trade_date ≤ 日期的最新值。
    # def universe_members_as_of(self, index_codes, as_of_dates):
    #     每个信号日的历史指数成分并集，不支持时不得伪装成当前成分。
    # def corporate_actions(self, codes, start=None, end=None):
    #     原始价持仓所需的分红送转与终止上市事件。
    # def industries_as_of(self, codes, as_of_dates):
    #     申万2021历史行业归属；不存在覆盖时返回「未知」而非当前行业。


# ---------------------------------------------------------------------------
# 自适应类型转换：把 duck-typed 对象（ORM 行 / dict）归一为本模块数据类
# ---------------------------------------------------------------------------


def _attr(obj: object, *names: str) -> object:
    """按候选名取属性/键，全部缺失返回 None。"""
    for name in names:
        if isinstance(obj, dict) and name in obj:
            return obj[name]
        if hasattr(obj, name):
            return getattr(obj, name)
    return None


def _to_float(value: object) -> float | None:
    if value is None:
        return None
    try:
        parsed = float(value)  # type: ignore[arg-type]
        return parsed if math.isfinite(parsed) else None
    except (TypeError, ValueError):
        return None


def _to_date(value: object) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value[:10])
        except ValueError:
            return None
    return None


def _to_compact_date(value: object) -> date | None:
    """兼容 Tushare YYYYMMDD 与 ISO 日期。"""
    if value is None:
        return None
    text = str(value)
    if len(text) >= 8 and text[:8].isdigit():
        try:
            return date(int(text[:4]), int(text[4:6]), int(text[6:8]))
        except ValueError:
            return None
    return _to_date(value)


def _to_bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    if isinstance(value, (int, float)):
        return value != 0
    return str(value).lower() in {"1", "true", "yes", "停牌", "suspended"}


def coerce_bar(obj: object) -> StockBar | None:
    """把 duck-typed 行情行转换为 StockBar；关键字段缺失返回 None。"""
    code = _attr(obj, "code", "stock_code", "symbol", "ts_code")
    trade_date = _to_date(_attr(obj, "trade_date", "date", "bar_date", "quote_date"))
    close = _to_float(_attr(obj, "close", "close_price", "raw_close", "price"))
    if code is None or trade_date is None or close is None or close <= 0:
        return None
    return StockBar(
        code=str(code),
        trade_date=trade_date,
        open=_to_float(_attr(obj, "open", "open_price")),
        high=_to_float(_attr(obj, "high", "high_price")),
        low=_to_float(_attr(obj, "low", "low_price")),
        close=close,
        volume=_to_float(_attr(obj, "volume", "vol")),
        amount=_to_float(_attr(obj, "amount", "turnover", "money")),
        suspended=_to_bool(_attr(obj, "suspended", "is_suspended", "halt")),
        raw_return=_to_float(_attr(obj, "raw_return", "pct_change", "pct_chg")),
        up_limit=_to_float(_attr(obj, "up_limit")),
        down_limit=_to_float(_attr(obj, "down_limit")),
    )


def coerce_info(obj: object) -> StockInfo | None:
    """把 duck-typed 元信息行转换为 StockInfo；关键字段缺失返回 None。"""
    code = _attr(obj, "code", "stock_code", "symbol", "ts_code")
    name = _attr(obj, "name", "stock_name", "short_name")
    if code is None:
        return None
    return StockInfo(
        code=str(code),
        name=str(name) if name is not None else str(code),
        industry=str(_attr(obj, "industry", "sw_industry", "sector") or "未知"),
        list_date=_to_date(_attr(obj, "list_date", "listing_date", "ipo_date")),
    )


def coerce_fundamentals(obj: object) -> Fundamentals | None:
    """把 duck-typed 财务行转换为 Fundamentals；关键字段缺失返回 None。"""
    code = _attr(obj, "code", "stock_code", "symbol", "ts_code")
    available_at = _to_date(
        _attr(obj, "available_at", "announce_date", "pub_date", "disclosure_date")
    )
    if code is None or available_at is None:
        return None
    return Fundamentals(
        code=str(code),
        available_at=available_at,
        period=_to_date(_attr(obj, "period", "report_date", "end_date")),
        roe=_to_float(_attr(obj, "roe", "roe_ttm")),
        gross_margin=_to_float(_attr(obj, "gross_margin", "grossprofit_margin")),
        ocf_to_profit=_to_float(
            _attr(obj, "ocf_to_profit", "ocf_to_np", "cash_to_profit")
        ),
        debt_ratio=_to_float(_attr(obj, "debt_ratio", "debt_to_assets", "leverage")),
        ep=_to_float(_attr(obj, "ep", "earnings_yield", "e_p")),
        bp=_to_float(_attr(obj, "bp", "book_to_price", "b_p")),
        market_cap=_to_float(_attr(obj, "market_cap", "total_mv")),
        float_market_cap=_to_float(_attr(obj, "float_market_cap", "circ_mv")),
        roa=_to_float(_attr(obj, "roa", "roa_dp")),
        net_margin=_to_float(_attr(obj, "net_margin", "netprofit_margin")),
        revenue=_to_float(_attr(obj, "revenue", "total_revenue")),
        net_income=_to_float(_attr(obj, "net_income", "n_income_attr_p")),
        operating_cash_flow=_to_float(
            _attr(obj, "operating_cash_flow", "n_cashflow_act")
        ),
        free_cash_flow=_to_float(_attr(obj, "free_cash_flow", "free_cashflow")),
        total_assets=_to_float(_attr(obj, "total_assets")),
        total_equity=_to_float(_attr(obj, "total_equity")),
        dividend_yield=_to_float(_attr(obj, "dividend_yield", "dv_ttm")),
        sales_yield=_to_float(_attr(obj, "sales_yield")),
        company_type=(
            str(_attr(obj, "company_type", "comp_type"))
            if _attr(obj, "company_type", "comp_type") is not None
            else None
        ),
        bank_net_interest_margin=_to_float(
            _attr(obj, "bank_net_interest_margin")
        ),
        bank_npl_ratio=_to_float(_attr(obj, "bank_npl_ratio")),
        bank_provision_coverage_ratio=_to_float(
            _attr(obj, "bank_provision_coverage_ratio")
        ),
        bank_capital_adequacy_ratio=_to_float(
            _attr(obj, "bank_capital_adequacy_ratio")
        ),
        bank_loan_deposit_ratio=_to_float(
            _attr(obj, "bank_loan_deposit_ratio")
        ),
        broker_proprietary_risk_ratio=_to_float(
            _attr(obj, "broker_proprietary_risk_ratio")
        ),
        broker_leverage_ratio=_to_float(_attr(obj, "broker_leverage_ratio")),
        broker_net_capital_ratio=_to_float(
            _attr(obj, "broker_net_capital_ratio")
        ),
        insurance_solvency_ratio=_to_float(
            _attr(obj, "insurance_solvency_ratio")
        ),
        insurance_combined_ratio=_to_float(
            _attr(obj, "insurance_combined_ratio")
        ),
        insurance_reserve_coverage_ratio=_to_float(
            _attr(obj, "insurance_reserve_coverage_ratio")
        ),
    )


# ---------------------------------------------------------------------------
# 内置 SQL 适配器：现有数据层（ORM + Parquet 数据湖 + DuckDB 研究仓库）
# ---------------------------------------------------------------------------

# 财务 payload（新浪原始 JSON）中候选中文字段名 → 归一化字段
_PAYLOAD_KEYS: dict[str, tuple[str, ...]] = {
    "gross_margin": (
        "销售毛利率(%)",
        "销售毛利率",
        "gross_margin",
        "grossprofit_margin",
        "XSMLL",
        "sale_gross_margin",
    ),
    "debt_ratio": (
        "资产负债率(%)",
        "资产负债率",
        "debt_ratio",
        "debt_to_assets",
        "ZCFZL",
        "assets_debt_ratio",
    ),
    "net_profit": (
        "净利润(元)",
        "净利润",
        "归属于母公司所有者的净利润(元)",
        "归属于母公司所有者的净利润",
        "net_profit",
        "PARENTNETPROFIT",
    ),
    "ocf": (
        "经营活动产生的现金流量净额(元)",
        "经营活动产生的现金流量净额",
        "ocf",
    ),
    "ocf_to_profit": ("ocf_to_profit", "NCO_NETPROFIT"),
}
# ROE 列与 payload 的候选键（百分数口径，/100 换算小数）
_ROE_KEYS = ("净资产收益率(%)", "净资产收益率", "roe", "roe_ttm")
# DuckDB fundamentals 数据集的 metric 名候选（百分数/倍数原样存储）
_METRIC_KEYS: dict[str, tuple[str, ...]] = {
    "roe": ("roe", "roe_ttm", "净资产收益率(%)"),
    "gross_margin": ("gross_margin", "销售毛利率(%)"),
    "debt_ratio": ("debt_ratio", "资产负债率(%)"),
    "ocf_to_profit": ("ocf_to_profit", "ocf_to_np"),
    "ep": ("ep", "earnings_yield"),
    "bp": ("bp", "book_to_price"),
}

# 行业模型候选（未来接入申万/中信行业时的探测名单）
_INDUSTRY_MODELS = (
    "StockIndustry",
    "StockIndustryMember",
    "StockIndustryClassification",
)

# 停牌判定：成交量/成交额同时非正视为停牌（数据源无显式标记时的近似）
_SUSPEND_EPS = 1e-9


def statutory_disclosure_deadline(period: date) -> date:
    """A股定期报告法定最晚披露日（保守 PIT 兜底口径）。

    - 年报（12-31）：次年 4 月 30 日；
    - 半年报（06-30）：当年 8 月 31 日；
    - 季报（03-31 / 09-30）：报告期后次月末（4-30 / 10-31）；
    - 其他非常规报告期：4 个月后的月末保守近似。
    """
    if period.month == 12 and period.day >= 28:
        return date(period.year + 1, 4, 30)
    if period.month == 6 and period.day >= 28:
        return date(period.year, 8, 31)
    if period.month == 3:
        return date(period.year, 4, 30)
    if period.month == 9:
        return date(period.year, 10, 31)
    month = period.month + 4
    year = period.year + (month - 1) // 12
    month = (month - 1) % 12 + 1
    if month == 12:
        return date(year, 12, 31)
    following = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
    return date.fromordinal(following.toordinal() - 1)


def _payload_value(payload: str | None, keys: tuple[str, ...]) -> float | None:
    """从财务 payload JSON 中按候选键取数值；无法解析返回 None。"""
    if not payload:
        return None
    try:
        data = json.loads(payload)
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    for key in keys:
        if key in data:
            value = _to_float(data[key])
            if value is not None:
                return value
    return None


def _as_date(value: object) -> date | None:
    return _to_date(value)


class SqlStockRepository:
    """现有数据层适配器（默认仓储）。

    数据源降级链：日线 = Parquet 数据湖 → DuckDB 研究仓库；
    行业 = 行业模型探测 → 「未知」；指数 = MarketIndex/IndexQuote。
    全部只读；任何单源缺失只影响对应字段（factor 缺失由因子层处理）。
    """

    def __init__(self, db: object, data_root: Path | None = None) -> None:
        self._db = db
        self._root = data_root
        self._external_snapshot_enabled = data_root is not None or (
            self._database_matches_config()
        )
        self._accessed_files: dict[str, dict[str, object]] = {}
        self._warehouse_repo = self._try_warehouse()
        self._industry_map = self._load_industries()

    # ---- 可选数据源探测 -------------------------------------------------

    def _record_file(self, path: Path) -> Path:
        """在实际读取前记录相对路径、大小和内容哈希。"""
        if not path.is_file():
            return path
        resolved = path.resolve()
        key = str(resolved)
        if key in self._accessed_files:
            return path
        from app.config import get_settings
        from app.services.file_access_manifest import file_observation

        root = self._root or Path(get_settings().research_data_dir)
        self._accessed_files[key] = file_observation(resolved, root)
        return path

    def accessed_file_records(self) -> list[dict[str, object]]:
        return list(self._accessed_files.values())

    def _database_matches_config(self) -> bool:
        """只让配置主库自动挂载配置的数据湖，避免临时库串入生产快照。"""
        try:
            from sqlalchemy.engine import make_url

            from app.config import get_settings

            configured = make_url(get_settings().database_url)
            bound = make_url(str(self._db.get_bind().url))  # type: ignore[attr-defined]
            if configured.get_backend_name() != bound.get_backend_name():
                return False
            if configured.get_backend_name() == "sqlite":
                return (
                    Path(configured.database or "").resolve()
                    == Path(bound.database or "").resolve()
                )
            return configured.render_as_string(hide_password=True) == (
                bound.render_as_string(hide_password=True)
            )
        except Exception:  # noqa: BLE001
            return False

    def _try_warehouse(self) -> object | None:
        """DuckDB 研究仓库存在时以只读方式接入；任何失败返回 None。"""
        if not self._external_snapshot_enabled:
            return None
        try:
            from app.config import get_settings

            settings = get_settings()
            db_path = Path(settings.research_db)
            if not db_path.exists():
                return None
            from app.research.repository import DuckDBRepository
            from app.research.warehouse import ResearchWarehouse

            warehouse = ResearchWarehouse(
                db_path, Path(settings.research_data_dir), read_only=True
            )
            return DuckDBRepository(warehouse, auto_init=False)
        except Exception:  # noqa: BLE001 - 可选数据源，失败静默降级
            return None

    def _load_industries(self) -> dict[str, str]:
        """探测行业模型（候选名），返回 {code: industry}；缺失返回 {}。"""
        try:
            module = importlib.import_module("app.models")
        except Exception:  # noqa: BLE001
            return {}
        model = None
        for name in _INDUSTRY_MODELS:
            model = getattr(module, name, None)
            if model is not None:
                break
        if model is None:
            return {}
        code_col = next(
            (
                getattr(model, c)
                for c in ("code", "stock_code", "symbol")
                if hasattr(model, c)
            ),
            None,
        )
        industry_col = next(
            (
                getattr(model, c)
                for c in ("industry", "sw_industry", "industry_name", "sector")
                if hasattr(model, c)
            ),
            None,
        )
        if code_col is None or industry_col is None:
            return {}
        try:
            from sqlalchemy import select

            source_col = getattr(model, "source", None)
            if source_col is None:
                rows = self._db.execute(select(code_col, industry_col)).all()  # type: ignore[attr-defined]
                return {
                    str(code): str(industry)
                    for code, industry in rows
                    if code and industry
                }
            rows = self._db.execute(  # type: ignore[attr-defined]
                select(code_col, industry_col, source_col)
            ).all()
            priority = {
                "stocktoday_sw2021": 100,
                "em": 80,
                "ths": 60,
                "cninfo_profile": 40,
                "cninfo": 20,
            }
            selected: dict[str, tuple[int, str]] = {}
            for code, industry, source in rows:
                if not code or not industry:
                    continue
                score = priority.get(str(source), 0)
                current = selected.get(str(code))
                if current is None or score > current[0]:
                    selected[str(code)] = (score, str(industry))
            return {code: industry for code, (_score, industry) in selected.items()}
        except Exception:  # noqa: BLE001 - 表结构不符预期时降级
            return {}

    # ---- 清单 ------------------------------------------------------------

    def list_stocks(self, codes: list[str] | None = None) -> list[StockInfo]:
        from sqlalchemy import select

        from app.models import StockMaster, StockUniverseSnapshot

        basic: dict[str, dict[str, object]] = {}
        if codes and self._external_snapshot_enabled:
            try:
                import pyarrow.parquet as pq

                from app.config import get_settings

                research_root = self._root or Path(get_settings().research_data_dir)
                wanted = set(codes)
                directory = (
                    research_root / "tushare_snapshot" / "global" / "stock_basic_full"
                )
                for path in sorted(directory.glob("*.parquet")):
                    self._record_file(path)
                    table = pq.read_table(
                        path,
                        columns=[
                            "ts_code",
                            "name",
                            "industry",
                            "list_date",
                        ],
                    )
                    for raw in table.to_pylist():
                        code = str(raw.get("ts_code") or "").split(".")[0]
                        if code in wanted:
                            basic[code] = raw
            except Exception:  # noqa: BLE001
                logger.warning("读取 Tushare 完整证券主数据失败", exc_info=True)

        stmt = select(StockMaster).order_by(StockMaster.code)
        if codes:
            stmt = stmt.where(StockMaster.code.in_(codes))
        result: list[StockInfo] = []
        found: set[str] = set()
        for row in self._db.execute(stmt).scalars().all():  # type: ignore[attr-defined]
            raw = basic.get(row.code, {})
            result.append(
                StockInfo(
                    code=row.code,
                    name=str(raw.get("name") or row.name),
                    industry=self._industry_map.get(row.code, "未知"),
                    list_date=_to_compact_date(raw.get("list_date")),
                )
            )
            found.add(row.code)
        missing_codes = set(codes or ()) - found
        historical_names: dict[str, str] = {}
        if missing_codes:
            name_rows = self._db.execute(  # type: ignore[attr-defined]
                select(
                    StockUniverseSnapshot.stock_code,
                    StockUniverseSnapshot.stock_name,
                    StockUniverseSnapshot.snapshot_date,
                )
                .where(StockUniverseSnapshot.stock_code.in_(missing_codes))
                .order_by(StockUniverseSnapshot.snapshot_date)
            ).all()
            historical_names = {
                str(code): str(name) for code, name, _snapshot_date in name_rows if name
            }
        alias_names: dict[str, str] = {}
        if missing_codes:
            try:
                from app.models import QuantDataRecord

                alias_rows = self._db.scalars(  # type: ignore[attr-defined]
                    select(QuantDataRecord).where(
                        QuantDataRecord.dataset == "corporate_action",
                        QuantDataRecord.code.in_(missing_codes),
                    )
                ).all()
                for record in alias_rows:
                    payload = dict(record.payload or {})
                    if payload.get("kind") not in {"code_change", "merger"}:
                        continue
                    old_name = str(payload.get("old_name") or "").strip()
                    successor = str(payload.get("successor_code") or "").split(".")[0]
                    if old_name:
                        alias_names[record.code] = old_name
                    elif successor:
                        successor_row = self._db.get(  # type: ignore[attr-defined]
                            StockMaster, successor
                        )
                        if successor_row is not None:
                            alias_names[record.code] = successor_row.name
            except Exception:  # noqa: BLE001 - 未迁移的旧库尚无规范化表
                pass
        for code in sorted(missing_codes):
            raw = basic.get(code)
            if raw is None and code not in historical_names and code not in alias_names:
                continue
            raw = raw or {}
            result.append(
                StockInfo(
                    code=code,
                    name=str(
                        alias_names.get(code)
                        or raw.get("name")
                        or historical_names.get(code)
                        or code
                    ),
                    industry=self._industry_map.get(
                        code, str(raw.get("industry") or "未知")
                    ),
                    list_date=_to_compact_date(raw.get("list_date")),
                )
            )
        return sorted(result, key=lambda item: item.code)

    # ---- 日线行情 ---------------------------------------------------------

    def _stocktoday_file(self, dataset: str, code: str) -> Path | None:
        """定位当前股票的 StockToday 原始分区文件。"""
        if not self._external_snapshot_enabled:
            return None
        try:
            from app.config import get_settings

            research_root = self._root or Path(get_settings().research_data_dir)
            directory = research_root / "tushare_snapshot" / "stocks" / dataset
            matches = sorted(directory.glob(f"{code}.*.parquet"))
            return self._record_file(matches[0]) if matches else None
        except Exception:  # noqa: BLE001
            return None

    def _execution_overrides(
        self, code: str, start: date | None, end: date | None
    ) -> tuple[dict[date, tuple[float | None, float | None]], set[date]]:
        """读取真实涨跌停价和停牌日；原始文件缺失时返回空覆盖。"""
        try:
            import pyarrow.parquet as pq

            limits: dict[date, tuple[float | None, float | None]] = {}
            limit_path = self._stocktoday_file("stk_limit", code)
            if limit_path is not None:
                table = pq.read_table(
                    limit_path,
                    columns=["trade_date", "up_limit", "down_limit"],
                )
                for raw_day, up, down in zip(
                    table.column("trade_date").to_pylist(),
                    table.column("up_limit").to_pylist(),
                    table.column("down_limit").to_pylist(),
                    strict=True,
                ):
                    text = str(raw_day)
                    day = _to_date(f"{text[:4]}-{text[4:6]}-{text[6:8]}")
                    if day is None:
                        continue
                    if start is not None and day < start:
                        continue
                    if end is not None and day > end:
                        continue
                    limits[day] = (_to_float(up), _to_float(down))

            suspended: set[date] = set()
            suspend_path = self._stocktoday_file("suspend_d", code)
            if suspend_path is not None:
                table = pq.read_table(
                    suspend_path, columns=["trade_date", "suspend_type"]
                )
                for raw_day, suspend_type in zip(
                    table.column("trade_date").to_pylist(),
                    table.column("suspend_type").to_pylist(),
                    strict=True,
                ):
                    if str(suspend_type).upper() != "S":
                        continue
                    text = str(raw_day)
                    day = _to_date(f"{text[:4]}-{text[4:6]}-{text[6:8]}")
                    if day is None:
                        continue
                    if start is not None and day < start:
                        continue
                    if end is not None and day > end:
                        continue
                    suspended.add(day)
            return limits, suspended
        except Exception:  # noqa: BLE001 - 可选执行数据损坏时退回行情推断
            logger.warning(
                "读取 %s 的真实涨跌停/停牌数据失败，回退行情推断",
                code,
                exc_info=True,
            )
            return {}, set()

    def corporate_actions(
        self,
        codes: list[str],
        start: date | None = None,
        end: date | None = None,
    ) -> list[CorporateAction]:
        """读取实施状态的分红送转，并为已退市证券生成终止持仓事件。

        分红在除权除息日计入持仓权益，避免原始价在除权日制造虚假亏损。
        当前主数据没有交易所正式退市日字段时，仅对名称含“退”且行情已终止
        的证券使用最后交易日收盘作为可审计的保守清算口径。
        """
        actions: list[CorporateAction] = []
        try:
            import pyarrow.parquet as pq

            for code in codes:
                path = self._stocktoday_file("dividend", code)
                if path is None:
                    continue
                table = pq.read_table(
                    path,
                    columns=[
                        "div_proc",
                        "stk_div",
                        "cash_div",
                        "cash_div_tax",
                        "record_date",
                        "ex_date",
                        "pay_date",
                        "div_listdate",
                    ],
                )
                for row in table.to_pylist():
                    if str(row.get("div_proc") or "") != "实施":
                        continue
                    action_date = _to_compact_date(row.get("ex_date"))
                    if action_date is None:
                        continue
                    share_ratio = _to_float(row.get("stk_div")) or 0.0
                    # 账户税务必须从税前分红出发，不能把源数据的统一税后
                    # 口径误当成每个 FIFO 批次的最终个人税负。
                    cash = _to_float(row.get("cash_div"))
                    if cash is None:
                        cash = _to_float(row.get("cash_div_tax"))
                    cash = cash or 0.0
                    if share_ratio <= 0 and cash <= 0:
                        continue
                    payment_date = _to_compact_date(row.get("pay_date"))
                    # 送转权益在除权日已经属于原股东，必须在除权日同步增加
                    # 经济持仓，不能等到上市日才计入净值而制造期间虚假亏损。
                    share_date = action_date
                    event_key = ":".join(
                        (
                            code,
                            str(row.get("record_date") or ""),
                            str(row.get("ex_date") or ""),
                            f"{cash:.10g}",
                            f"{share_ratio:.10g}",
                        )
                    )
                    source = f"tushare:dividend:{path.name}"
                    if (
                        share_ratio > 0
                        and (start is None or share_date >= start)
                        and (end is None or share_date <= end)
                    ):
                        actions.append(
                            CorporateAction(
                                code=code,
                                action_date=share_date,
                                kind="share_distribution",
                                share_ratio=max(share_ratio, 0.0),
                                event_key=event_key,
                                source=source,
                            )
                        )
                    if (
                        cash > 0
                        and (start is None or action_date >= start)
                        and (end is None or action_date <= end)
                    ):
                        actions.append(
                            CorporateAction(
                                code=code,
                                action_date=action_date,
                                kind="cash_entitlement",
                                cash_per_share=max(cash, 0.0),
                                event_key=event_key,
                                payment_date=payment_date,
                                record_date=_to_compact_date(row.get("record_date")),
                                source=source,
                            )
                        )
                    if (
                        cash > 0
                        and payment_date is not None
                        and payment_date > action_date
                        and (start is None or payment_date >= start)
                        and (end is None or payment_date <= end)
                    ):
                        actions.append(
                            CorporateAction(
                                code=code,
                                action_date=payment_date,
                                kind="cash_payment",
                                event_key=event_key,
                                payment_date=payment_date,
                                source=source,
                            )
                        )
        except Exception:  # noqa: BLE001 - 损坏的原始表必须显式告警
            logger.warning("读取分红送转公司行为失败", exc_info=True)

        # 规范化层允许补录交易所公告中的配股、合并与现金收购事件。
        # 原始记录不可修改，payload 必须带明确 kind，因而人工修正仍可追溯。
        official_terminal_codes: set[str] = set()
        try:
            import pyarrow.parquet as pq

            if self._external_snapshot_enabled:
                from app.config import get_settings

                research_root = self._root or Path(get_settings().research_data_dir)
                delisted_path = (
                    research_root
                    / "tushare_snapshot"
                    / "global"
                    / "stock_basic_full"
                    / "D.parquet"
                )
                if delisted_path.exists():
                    table = pq.read_table(
                        delisted_path,
                        columns=["ts_code", "delist_date"],
                    )
                    wanted = set(codes)
                    for row in table.to_pylist():
                        code = str(row.get("ts_code") or "").split(".")[0]
                        terminal_day = _to_compact_date(row.get("delist_date"))
                        if code not in wanted or terminal_day is None:
                            continue
                        if start is not None and terminal_day < start:
                            continue
                        if end is not None and terminal_day > end:
                            continue
                        actions.append(
                            CorporateAction(
                                code=code,
                                action_date=terminal_day,
                                kind="terminal",
                                terminal_price=None,
                                terminal_type="unknown",
                                consideration_status="unknown",
                                restricted_valuation_per_share=0.0,
                                review_status="open",
                                event_key=f"{code}:{terminal_day}:delist",
                                source=(
                                    f"tushare:stock_basic_full:D:{delisted_path.name}"
                                ),
                            )
                        )
                        official_terminal_codes.add(code)
        except Exception:  # noqa: BLE001
            logger.warning("读取交易所退市日期失败", exc_info=True)

        try:
            from sqlalchemy import select

            from app.models import QuantDataRecord

            statement = select(QuantDataRecord).where(
                QuantDataRecord.dataset == "corporate_action",
                QuantDataRecord.code.in_(codes),
            )
            if start is not None:
                statement = statement.where(QuantDataRecord.effective_date >= start)
            if end is not None:
                statement = statement.where(QuantDataRecord.effective_date <= end)
            candidate_records = self._db.scalars(statement).all()  # type: ignore[attr-defined]
            chosen_records: dict[tuple[str, date, str, str], object] = {}
            for record in candidate_records:
                candidate_payload = dict(record.payload or {})
                if candidate_payload.get("resolution_status") in {
                    "conflict",
                    "rejected",
                    "superseded",
                }:
                    continue
                candidate_key = (
                    record.code,
                    record.effective_date,
                    str(candidate_payload.get("kind") or ""),
                    str(candidate_payload.get("event_key") or record.effective_date),
                )
                previous = chosen_records.get(candidate_key)
                if previous is None or (
                    int(candidate_payload.get("revision") or 1),
                    record.available_at,
                    record.id,
                ) > (
                    int(
                        dict(previous.payload or {}).get("revision")  # type: ignore[union-attr]
                        or 1
                    ),
                    previous.available_at,  # type: ignore[union-attr]
                    previous.id,  # type: ignore[union-attr]
                ):
                    chosen_records[candidate_key] = record
            for record in chosen_records.values():
                payload = dict(record.payload or {})
                kind = str(payload.get("kind") or "").strip()
                if kind not in {
                    "rights_issue",
                    "merger",
                    "code_change",
                    "share_distribution",
                    "cash_entitlement",
                    "cash_payment",
                    "terminal",
                    "cash_acquisition",
                }:
                    continue
                normalized_kind = "terminal" if kind == "cash_acquisition" else kind
                actions.append(
                    CorporateAction(
                        code=record.code,
                        action_date=record.effective_date,
                        kind=normalized_kind,
                        cash_per_share=_to_float(payload.get("cash_per_share")) or 0.0,
                        share_ratio=_to_float(payload.get("share_ratio")) or 0.0,
                        terminal_price=_to_float(payload.get("terminal_price")),
                        event_key=str(payload.get("event_key") or record.id),
                        payment_date=_to_compact_date(payload.get("payment_date")),
                        subscription_ratio=(
                            _to_float(payload.get("subscription_ratio")) or 0.0
                        ),
                        subscription_price=_to_float(payload.get("subscription_price")),
                        successor_code=(
                            str(payload.get("successor_code")).split(".")[0]
                            if payload.get("successor_code")
                            else None
                        ),
                        record_date=_to_compact_date(payload.get("record_date")),
                        terminal_type=(
                            str(payload.get("terminal_type"))
                            if payload.get("terminal_type")
                            else (
                                "cash_acquisition"
                                if kind == "cash_acquisition"
                                else None
                            )
                        ),
                        consideration_status=str(
                            payload.get("consideration_status") or "unknown"
                        ),
                        restricted_valuation_per_share=(
                            _to_float(payload.get("restricted_valuation_per_share"))
                            or 0.0
                        ),
                        review_status=str(payload.get("review_status") or "unreviewed"),
                        rights_tradable=bool(payload.get("rights_tradable", False)),
                        right_market_price=_to_float(payload.get("right_market_price")),
                        subscription_deadline=_to_compact_date(
                            payload.get("subscription_deadline")
                        ),
                        successor_listing_date=_to_compact_date(
                            payload.get("successor_listing_date")
                        ),
                        cash_compensation_per_fraction=_to_float(
                            payload.get("cash_compensation_per_fraction")
                        ),
                        fractional_handling=str(
                            payload.get("fractional_handling")
                            or "cash_if_official_else_restricted"
                        ),
                        source_hash=record.source_hash,
                        revision=int(payload.get("revision") or 1),
                        source=(
                            f"normalized:{record.source}:{record.source_file}:"
                            f"{record.source_hash}"
                        ),
                    )
                )
        except Exception:  # noqa: BLE001 - 旧库未迁移时不影响原始源读取
            logger.warning("读取规范化公司行为失败", exc_info=True)

        try:
            from sqlalchemy import select

            from app.models import StockDailyBar, StockMaster

            rows = self._db.execute(  # type: ignore[attr-defined]
                select(
                    StockMaster.code,
                    StockMaster.name,
                    StockDailyBar.last_trade_date,
                )
                .join(StockDailyBar, StockDailyBar.code == StockMaster.code)
                .where(
                    StockMaster.code.in_(codes),
                    StockMaster.name.contains("退"),
                    StockDailyBar.last_trade_date.is_not(None),
                )
            ).all()
            for code, _name, terminal_day in rows:
                if str(code) in official_terminal_codes:
                    continue
                if start is not None and terminal_day < start:
                    continue
                if end is not None and terminal_day > end:
                    continue
                actions.append(
                    CorporateAction(
                        code=str(code),
                        action_date=terminal_day,
                        kind="terminal",
                        terminal_price=None,
                        terminal_type="unknown",
                        consideration_status="unknown",
                        restricted_valuation_per_share=0.0,
                        review_status="open",
                        event_key=f"{code}:{terminal_day}:unverified_delist",
                        source="stock_master:退+stock_daily_bars:last_trade_date",
                    )
                )
        except Exception:  # noqa: BLE001 - 旧数据库无相应元数据时仅缺少终止事件
            logger.warning("读取退市终止事件失败", exc_info=True)

        # 规范化主数据存在时优先消费其版本，禁止和静态 Parquet 同一事件
        # 重复入账；静态源只作为尚未完成规范化导入时的可追溯降级。
        normalized_keys = {
            (action.code, action.action_date, action.kind)
            for action in actions
            if action.source.startswith("normalized:")
        }
        actions = [
            action
            for action in actions
            if action.source.startswith("normalized:")
            or (action.code, action.action_date, action.kind) not in normalized_keys
        ]
        # 原始源偶有同一实施方案重复行；按自然键去重并保持确定顺序。
        unique = {
            (
                action.code,
                action.action_date,
                action.kind,
                action.cash_per_share,
                action.share_ratio,
                action.event_key,
                action.subscription_ratio,
                action.subscription_price,
                action.successor_code,
            ): action
            for action in actions
        }
        return sorted(
            unique.values(), key=lambda action: (action.action_date, action.code)
        )

    def industries_as_of(
        self,
        codes: list[str],
        as_of_dates: list[date] | tuple[date, ...],
    ) -> dict[date, dict[str, str]]:
        """按申万2021成员的 in_date/out_date 返回历史一级行业。"""
        dates = tuple(sorted(set(as_of_dates)))
        result: dict[date, dict[str, str]] = {day: {} for day in dates}
        if not dates:
            return result
        try:
            import pyarrow.parquet as pq

            for code in codes:
                path = self._stocktoday_file("index_member_all", code)
                if path is None:
                    continue
                table = pq.read_table(
                    path,
                    columns=["l1_name", "in_date", "out_date"],
                )
                periods: list[tuple[date, date | None, str]] = []
                for row in table.to_pylist():
                    industry = str(row.get("l1_name") or "").strip()
                    in_date = _to_compact_date(row.get("in_date"))
                    out_date = _to_compact_date(row.get("out_date"))
                    if industry and in_date is not None:
                        periods.append((in_date, out_date, industry))
                for day in dates:
                    active = [
                        period
                        for period in periods
                        if period[0] <= day and (period[1] is None or day <= period[1])
                    ]
                    if active:
                        result[day][code] = max(active, key=lambda period: period[0])[2]
        except Exception:  # noqa: BLE001
            logger.warning("读取申万2021历史行业成员失败", exc_info=True)
        return result

    def _bars_from_parquet(
        self, code: str, start: date | None, end: date | None, layer: str | None = None
    ) -> list[StockBar]:
        try:
            from app.services.research import parquet_store

            path = parquet_store.daily_path(
                code,
                layer or parquet_store.DAILY_RAW,
                self._root,
            )
            if path.is_file():
                self._record_file(path)
            frame = parquet_store.read_daily(
                code, layer or parquet_store.DAILY_RAW, self._root
            )
        except Exception:  # noqa: BLE001 - 数据湖读取失败按无数据处理
            return []
        if frame is None:
            return []
        limits: dict[date, tuple[float | None, float | None]] = {}
        explicit_suspensions: set[date] = set()
        if layer is None or layer == parquet_store.DAILY_RAW:
            limits, explicit_suspensions = self._execution_overrides(code, start, end)
        bars: list[StockBar] = []
        for record in frame.to_dict(orient="records"):
            trade_date = _as_date(record.get("trade_date"))
            close = _to_float(record.get("close"))
            if trade_date is None or close is None or close <= 0:
                continue
            if start is not None and trade_date < start:
                continue
            if end is not None and trade_date > end:
                continue
            volume = _to_float(record.get("volume"))
            amount = _to_float(record.get("amount"))
            suspended = trade_date in explicit_suspensions or (
                (volume is None or volume <= _SUSPEND_EPS)
                and (amount is None or amount <= _SUSPEND_EPS)
            )
            up_limit, down_limit = limits.get(trade_date, (None, None))
            bars.append(
                StockBar(
                    code=code,
                    trade_date=trade_date,
                    open=_to_float(record.get("open")),
                    high=_to_float(record.get("high")),
                    low=_to_float(record.get("low")),
                    close=close,
                    volume=volume,
                    amount=amount,
                    suspended=suspended,
                    raw_return=_to_float(record.get("pct_change")),
                    up_limit=up_limit,
                    down_limit=down_limit,
                )
            )
        return bars

    def _bars_from_stocktoday(
        self, code: str, start: date | None, end: date | None
    ) -> list[StockBar]:
        """研究湖缺失时读取 Tushare 原始日线，保持执行价未经复权。"""
        path = self._stocktoday_file("daily", code)
        if path is None:
            return []
        try:
            import pyarrow.parquet as pq

            limits, explicit_suspensions = self._execution_overrides(code, start, end)
            bars: list[StockBar] = []
            for record in pq.read_table(path).to_pylist():
                trade_date = _to_compact_date(record.get("trade_date"))
                close = _to_float(record.get("close"))
                if trade_date is None or close is None or close <= 0:
                    continue
                if start is not None and trade_date < start:
                    continue
                if end is not None and trade_date > end:
                    continue
                volume = _to_float(record.get("vol"))
                if volume is None:
                    volume = _to_float(record.get("volume"))
                amount = _to_float(record.get("amount"))
                suspended = trade_date in explicit_suspensions or (
                    (volume is None or volume <= _SUSPEND_EPS)
                    and (amount is None or amount <= _SUSPEND_EPS)
                )
                up_limit, down_limit = limits.get(trade_date, (None, None))
                pct_change = _to_float(record.get("pct_chg"))
                if pct_change is None:
                    pct_change = _to_float(record.get("pct_change"))
                bars.append(
                    StockBar(
                        code=code,
                        trade_date=trade_date,
                        open=_to_float(record.get("open")),
                        high=_to_float(record.get("high")),
                        low=_to_float(record.get("low")),
                        close=close,
                        volume=volume,
                        amount=amount,
                        suspended=suspended,
                        raw_return=(
                            pct_change / 100.0 if pct_change is not None else None
                        ),
                        up_limit=up_limit,
                        down_limit=down_limit,
                    )
                )
            return sorted(bars, key=lambda bar: bar.trade_date)
        except Exception:  # noqa: BLE001
            logger.warning("读取 %s 的 Tushare 原始日线失败", code, exc_info=True)
            return []

    def _adjust_stocktoday_bars(
        self, code: str, bars: list[StockBar]
    ) -> list[StockBar]:
        """用原始 adj_factor 构造前复权研究序列，执行序列保持 raw。"""
        factor_path = self._stocktoday_file("adj_factor", code)
        if factor_path is None or not bars:
            return []
        try:
            import pyarrow.parquet as pq

            factors = {
                day: value
                for record in pq.read_table(factor_path).to_pylist()
                if (day := _to_compact_date(record.get("trade_date"))) is not None
                and (value := _to_float(record.get("adj_factor"))) is not None
                and value > 0
            }
            latest_factor = factors.get(bars[-1].trade_date)
            if latest_factor is None:
                eligible = [
                    (day, value)
                    for day, value in factors.items()
                    if day <= bars[-1].trade_date
                ]
                latest_factor = (
                    max(eligible, key=lambda item: item[0])[1] if eligible else None
                )
            if latest_factor is None or latest_factor <= 0:
                return []
            adjusted: list[StockBar] = []
            for bar in bars:
                factor = factors.get(bar.trade_date)
                if factor is None:
                    return []
                scale = factor / latest_factor
                adjusted.append(
                    StockBar(
                        code=bar.code,
                        trade_date=bar.trade_date,
                        open=(bar.open * scale if bar.open is not None else None),
                        high=(bar.high * scale if bar.high is not None else None),
                        low=bar.low * scale if bar.low is not None else None,
                        close=bar.close * scale,
                        volume=bar.volume,
                        amount=bar.amount,
                        suspended=bar.suspended,
                        raw_return=bar.raw_return,
                        up_limit=None,
                        down_limit=None,
                    )
                )
            return adjusted
        except Exception:  # noqa: BLE001
            logger.warning("用 adj_factor 复权 %s 失败", code, exc_info=True)
            return []

    def _bars_from_warehouse(
        self, codes: list[str] | None, start: date | None, end: date | None
    ) -> list[StockBar]:
        if self._warehouse_repo is None:
            return []
        try:
            frame = self._warehouse_repo.read_stock_daily(codes, start=start, end=end)  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            return []
        if frame is None or frame.empty:
            return []
        bars: list[StockBar] = []
        for record in frame.to_dict(orient="records"):
            bar = coerce_bar(
                {
                    "code": record.get("symbol"),
                    "trade_date": record.get("effective_date"),
                    "open": record.get("open"),
                    "high": record.get("high"),
                    "low": record.get("low"),
                    "close": record.get("close"),
                    "volume": record.get("volume"),
                    "amount": record.get("amount"),
                    "raw_return": (
                        (record.get("pct_change") or 0) / 100.0
                        if record.get("pct_change") is not None
                        else None
                    ),
                }
            )
            if bar is not None:
                bars.append(bar)
        return bars

    def daily_bars(
        self,
        codes: list[str] | None = None,
        start: date | None = None,
        end: date | None = None,
    ) -> list[StockBar]:
        """日线行情：Parquet 数据湖优先，缺失的股票回退 DuckDB 研究仓库。"""
        if codes is None:
            codes = [info.code for info in self.list_stocks(None)]
        bars: list[StockBar] = []
        missing: list[str] = []
        for code in codes:
            series = self._bars_from_parquet(code, start, end)
            if not series:
                series = self._bars_from_stocktoday(code, start, end)
            if series:
                bars.extend(series)
            else:
                missing.append(code)
        if missing:
            bars.extend(self._bars_from_warehouse(missing, start, end))
        return bars

    def market_bars(
        self,
        codes: list[str],
        start: date | None = None,
        end: date | None = None,
    ) -> dict[str, MarketBars]:
        """双口径面板：研究口径优先 qfq（缺失回退 raw），执行口径恒 raw。"""
        from app.services.research import parquet_store

        exec_map: dict[str, list[StockBar]] = {}
        for bar in self.daily_bars(codes or None, start, end):
            exec_map.setdefault(bar.code, []).append(bar)
        panel: dict[str, MarketBars] = {}
        for code in codes:
            exec_bars = sorted(exec_map.get(code, []), key=lambda bar: bar.trade_date)
            research_bars = self._bars_from_parquet(
                code, start, end, layer=parquet_store.DAILY_QFQ
            )
            if not research_bars:
                research_bars = self._adjust_stocktoday_bars(code, exec_bars)
            if not research_bars:
                research_bars = list(exec_bars)
            elif exec_bars and research_bars[-1].trade_date < exec_bars[-1].trade_date:
                # 收盘快照先推进 raw；qfq 深度刷新稍后完成。用数据源涨跌幅
                # 把研究价格链延伸到最新日，避免信号滞后一天。涨跌幅缺失时
                # 才回退 raw 收盘比（除权日可能有偏差，后续 qfq 刷新会覆盖）。
                extended = list(research_bars)
                previous_raw: StockBar | None = None
                for raw in exec_bars:
                    if raw.trade_date <= research_bars[-1].trade_date:
                        previous_raw = raw
                        continue
                    change = raw.raw_return
                    if (
                        change is None
                        and previous_raw is not None
                        and previous_raw.close > 0
                    ):
                        change = raw.close / previous_raw.close - 1.0
                    if change is None:
                        previous_raw = raw
                        continue
                    close = extended[-1].close * (1.0 + change)
                    scale = close / raw.close if raw.close > 0 else 1.0
                    extended.append(
                        StockBar(
                            code=code,
                            trade_date=raw.trade_date,
                            open=raw.open * scale if raw.open is not None else None,
                            high=raw.high * scale if raw.high is not None else None,
                            low=raw.low * scale if raw.low is not None else None,
                            close=close,
                            volume=raw.volume,
                            amount=raw.amount,
                            suspended=raw.suspended,
                            raw_return=change,
                            up_limit=raw.up_limit,
                            down_limit=raw.down_limit,
                        )
                    )
                    previous_raw = raw
                research_bars = extended
            panel[code] = MarketBars(
                research_bars=tuple(research_bars), exec_bars=tuple(exec_bars)
            )
        return panel

    def name_histories(self, codes: list[str]) -> dict[str, list[NamePeriod]]:
        """历史名称/ST 区间（stock_name_history 表，按 start_date/sort_order 升序）。

        数据源不披露精确日期时 start_date 为空：该区间不参与 as_of 覆盖
        判定（跳过），ST 判定回退当前名称 —— 不伪造日期、不错误摘帽。
        """
        from sqlalchemy import select

        from app.models import StockNameHistory

        rows = (
            self._db.execute(  # type: ignore[attr-defined]
                select(StockNameHistory)
                .where(StockNameHistory.code.in_(codes))
                .order_by(StockNameHistory.code, StockNameHistory.sort_order)
            )
            .scalars()
            .all()
        )
        result: dict[str, list[NamePeriod]] = {}
        for row in rows:
            if row.start_date is None:
                continue  # 无精确日期的区间不参与 as_of 判定（不伪造）
            result.setdefault(row.code, []).append(
                NamePeriod(
                    code=row.code,
                    name=row.name,
                    start_date=row.start_date,
                    end_date=row.end_date,
                    is_st=bool(row.is_st),
                )
            )
        return result

    def trade_calendar(self, start: date | None, end: date | None) -> TradeCalendar:
        """交易日历：StockToday SSE 权威日历优先，行情日期推断仅回退。"""
        try:
            from app.config import get_settings
            import pyarrow.parquet as pq

            research_root = self._root or Path(get_settings().research_data_dir)
            calendar_path = (
                research_root
                / "tushare_snapshot"
                / "global"
                / "trade_cal"
                / "SSE.parquet"
            )
            if calendar_path.exists():
                self._record_file(calendar_path)
                table = pq.read_table(calendar_path, columns=["cal_date", "is_open"])
                days = []
                for raw_day, is_open in zip(
                    table.column("cal_date").to_pylist(),
                    table.column("is_open").to_pylist(),
                    strict=True,
                ):
                    day = _to_date(
                        f"{str(raw_day)[:4]}-{str(raw_day)[4:6]}-{str(raw_day)[6:8]}"
                    )
                    if not is_open or day is None:
                        continue
                    if start is not None and day < start:
                        continue
                    if end is not None and day > end:
                        continue
                    days.append(day)
                if days:
                    return TradeCalendar(tuple(sorted(set(days))))
        except Exception:  # noqa: BLE001 - 原始日历异常时显式告警后回退
            logger.warning(
                "StockToday trade_cal 读取失败，交易日历回退为行情日期并集",
                exc_info=True,
            )

        from sqlalchemy import select

        from app.models import StockDailyBar

        codes = list(self._db.execute(select(StockDailyBar.code)).scalars().all())  # type: ignore[attr-defined]
        if not codes:
            codes = [info.code for info in self.list_stocks(None)]
        days: set[date] = set()
        logger.warning("权威 trade_cal 不可用，交易日历由行情日期推断")
        # 抽样若干只高覆盖股票构造日历（全市场逐只读湖代价过高）
        for code in codes[:50]:
            for bar in self._bars_from_parquet(code, start, end):
                days.add(bar.trade_date)
            if len(days) > 3000:
                break
        if not days:
            for bar in self._bars_from_warehouse(None, start, end):
                days.add(bar.trade_date)
        return TradeCalendar(tuple(sorted(days)))

    def universe_members_as_of(
        self,
        index_codes: list[str] | tuple[str, ...],
        as_of_dates: list[date] | tuple[date, ...],
    ) -> dict[date, UniverseMembership]:
        """读取每个信号日不晚于该日的最近历史指数成分快照。

        一次装载所需区间的快照后在内存中二分定位，避免按“日期×指数”
        反复查询数据库。任一指数没有历史快照时会记录 missing_indices，
        调用方据此拒绝或显式降级，绝不回退到当前成分。
        """
        from sqlalchemy import select

        from app.models import StockUniverseSnapshot

        indices = tuple(dict.fromkeys(index_codes))
        dates = tuple(sorted(set(as_of_dates)))
        if not indices or not dates:
            return {}
        rows = self._db.execute(  # type: ignore[attr-defined]
            select(
                StockUniverseSnapshot.index_code,
                StockUniverseSnapshot.snapshot_date,
                StockUniverseSnapshot.stock_code,
            )
            .where(
                StockUniverseSnapshot.index_code.in_(indices),
                StockUniverseSnapshot.snapshot_date <= dates[-1],
            )
            .order_by(
                StockUniverseSnapshot.index_code,
                StockUniverseSnapshot.snapshot_date,
                StockUniverseSnapshot.stock_code,
            )
        ).all()
        snapshots: dict[str, dict[date, set[str]]] = {index: {} for index in indices}
        for index_code, snapshot_date, stock_code in rows:
            snapshots.setdefault(index_code, {}).setdefault(snapshot_date, set()).add(
                stock_code
            )

        available_dates = {
            index: sorted(by_date) for index, by_date in snapshots.items()
        }
        result: dict[date, UniverseMembership] = {}
        for as_of in dates:
            members: set[str] = set()
            used: dict[str, date] = {}
            missing: list[str] = []
            for index in indices:
                candidates = available_dates.get(index, [])
                position = bisect_right(candidates, as_of) - 1
                if position < 0:
                    missing.append(index)
                    continue
                snapshot_date = candidates[position]
                used[index] = snapshot_date
                members.update(snapshots[index][snapshot_date])
            result[as_of] = UniverseMembership(
                as_of=as_of,
                members=frozenset(members),
                snapshot_dates=used,
                missing_indices=tuple(missing),
            )
        return result

    # ---- PIT 财务 ----------------------------------------------------------

    def _disclosure_map(self, codes: list[str]) -> dict[tuple[str, date], date]:
        """(code, report_date) → 实际披露日（available_at 的精确口径）。"""
        from sqlalchemy import select

        from app.models import StockReportDisclosure

        rows = self._db.execute(  # type: ignore[attr-defined]
            select(
                StockReportDisclosure.code,
                StockReportDisclosure.report_date,
                StockReportDisclosure.disclosure_date,
                StockReportDisclosure.available_at,
            ).where(StockReportDisclosure.code.in_(codes))
        ).all()
        result: dict[tuple[str, date], date] = {}
        for code, report_date, disclosure_date, available_at in rows:
            day = _as_date(disclosure_date) or _as_date(available_at)
            if day is not None:
                result[(code, report_date)] = day
        return result

    def _valuation_latest(
        self, codes: list[str], as_of: date | None = None
    ) -> dict[str, dict[str, tuple[date, float]]]:
        """每只股票 as_of 时点最新 PE(TTM)/PB：(trade_date, value)。

        PIT 口径：仅取 trade_date ≤ as_of 的估值（as_of 为 None 时不过滤），
        避免未来 PE/PB 泄漏进历史打分。"""
        from sqlalchemy import and_, func, select

        from app.models import StockValuation

        latest_dates = select(
            StockValuation.code.label("code"),
            StockValuation.indicator.label("indicator"),
            func.max(StockValuation.trade_date).label("trade_date"),
        ).where(
            StockValuation.code.in_(codes),
            StockValuation.indicator.in_(("pe_ttm", "pb", "total_mv")),
        )
        if as_of is not None:
            latest_dates = latest_dates.where(StockValuation.trade_date <= as_of)
        latest_dates = latest_dates.group_by(
            StockValuation.code, StockValuation.indicator
        ).subquery()
        stmt = select(
            StockValuation.code,
            StockValuation.trade_date,
            StockValuation.indicator,
            StockValuation.value,
        ).join(
            latest_dates,
            and_(
                StockValuation.code == latest_dates.c.code,
                StockValuation.indicator == latest_dates.c.indicator,
                StockValuation.trade_date == latest_dates.c.trade_date,
            ),
        )
        rows = self._db.execute(stmt).all()  # type: ignore[attr-defined]
        latest: dict[str, dict[str, tuple[date, float]]] = {}
        for code, trade_date, indicator, value in rows:
            number = _to_float(value)
            day = _as_date(trade_date)
            if number is None or number <= 0 or day is None:
                continue
            entry = latest.setdefault(code, {})
            current = entry.get(indicator)
            if current is None or day > current[0]:
                entry[indicator] = (day, number)
        return latest

    def valuation_snapshots(
        self,
        codes: list[str],
        as_of_dates: list[date] | tuple[date, ...],
    ) -> list[Fundamentals]:
        """生成指定信号日的独立 PIT 估值快照。

        估值是每日市场数据，不能附着到季度财务报告上。调用方传入实际
        信号日，本方法逐日选取不晚于该日的 PE(TTM)/PB，并用信号日作为
        快照可用日。这样既避免未来数据，也只装载策略真正需要的月频观测，
        不必把数百万条日估值全部放入内存。
        """
        if not codes:
            return []
        result: list[Fundamentals] = []
        requested_dates = tuple(sorted(set(as_of_dates)))
        raw_by_date: dict[date, dict[str, dict[str, float]]] = {
            day: {} for day in requested_dates
        }
        raw_trade_dates: dict[date, dict[str, date]] = {
            day: {} for day in requested_dates
        }
        source_mismatch_counts: dict[str, int] = {}
        try:
            import pyarrow.parquet as pq

            research_root = self._root
            if research_root is None:
                from app.config import get_settings

                research_root = Path(get_settings().research_data_dir)
            monthly_directory = (
                research_root / "tushare_snapshot" / "global" / "daily_basic_monthly"
            )
            monthly_files = (
                sorted(monthly_directory.glob("*.parquet"))
                if self._external_snapshot_enabled
                else []
            )
            monthly_dates = [_to_compact_date(path.stem) for path in monthly_files]
            valid_monthly = [
                (day, path)
                for day, path in zip(monthly_dates, monthly_files, strict=True)
                if day is not None
            ]
            chosen_files: dict[Path, list[date]] = {}
            valid_days = [day for day, _path in valid_monthly]
            for as_of in requested_dates:
                position = bisect_right(valid_days, as_of) - 1
                if position >= 0:
                    chosen_files.setdefault(valid_monthly[position][1], []).append(
                        as_of
                    )
            wanted_codes = set(codes)
            for path, signal_days in chosen_files.items():
                path_day = _to_compact_date(path.stem)
                if path_day is None:
                    continue
                self._record_file(path)
                table = pq.read_table(
                    path,
                    columns=[
                        "ts_code",
                        "pe_ttm",
                        "pb",
                        "ps_ttm",
                        "dv_ttm",
                        "total_mv",
                        "circ_mv",
                    ],
                )
                values_by_code: dict[str, dict[str, float]] = {}
                for row in table.to_pylist():
                    code = str(row.get("ts_code") or "").split(".")[0]
                    if code not in wanted_codes:
                        continue
                    values_by_code[code] = {
                        key: number
                        for key in (
                            "pe_ttm",
                            "pb",
                            "ps_ttm",
                            "dv_ttm",
                            "total_mv",
                            "circ_mv",
                        )
                        if (number := _to_float(row.get(key))) is not None
                    }
                for as_of in signal_days:
                    raw_by_date[as_of].update(values_by_code)
                    raw_trade_dates[as_of].update(
                        {code: path_day for code in values_by_code}
                    )

            for code in codes:
                path = self._stocktoday_file("daily_basic", code)
                if path is None:
                    continue
                table = pq.read_table(
                    path,
                    columns=[
                        "trade_date",
                        "pe_ttm",
                        "pb",
                        "ps_ttm",
                        "dv_ttm",
                        "total_mv",
                        "circ_mv",
                    ],
                )
                rows = sorted(
                    (
                        (_to_compact_date(row.get("trade_date")), row)
                        for row in table.to_pylist()
                    ),
                    key=lambda pair: pair[0] or date.min,
                )
                valid_rows = [(day, row) for day, row in rows if day is not None]
                row_dates = [day for day, _row in valid_rows]
                for as_of in requested_dates:
                    position = bisect_right(row_dates, as_of) - 1
                    if position < 0:
                        continue
                    _day, row = valid_rows[position]
                    values = {
                        key: number
                        for key in (
                            "pe_ttm",
                            "pb",
                            "ps_ttm",
                            "dv_ttm",
                            "total_mv",
                            "circ_mv",
                        )
                        if (number := _to_float(row.get(key))) is not None
                    }
                    raw_by_date[as_of][code] = values
                    raw_trade_dates[as_of][code] = _day
        except Exception:  # noqa: BLE001
            logger.warning(
                "读取 daily_basic PIT 估值失败，回退规范化估值表", exc_info=True
            )

        # trailing dividend yield 的分母必须是信号日当时可成交的 raw 收盘价。
        prices_by_date: dict[date, dict[str, float]] = {
            day: {} for day in requested_dates
        }
        dividend_coverage = {
            code: self._stocktoday_file("dividend", code) is not None
            for code in codes
        }
        from app.services.dividend_yield import (
            calculate_trailing_dividend_yield,
            load_normalized_dividend_events,
        )

        bars_by_code: dict[str, list[StockBar]] = {}
        if requested_dates:
            all_bars = self.daily_bars(
                codes,
                start=requested_dates[0] - timedelta(days=15),
                end=requested_dates[-1],
            )
            for bar in all_bars:
                if bar.close > 0:
                    bars_by_code.setdefault(bar.code, []).append(bar)
            for code, series in bars_by_code.items():
                series.sort(key=lambda item: item.trade_date)
                days = [item.trade_date for item in series]
                for as_of in requested_dates:
                    position = bisect_right(days, as_of) - 1
                    if (
                        position >= 0
                        and (as_of - series[position].trade_date).days <= 15
                    ):
                        prices_by_date[as_of][code] = series[position].close
        try:
            all_dividend_events: list[object] = load_normalized_dividend_events(
                self._db,  # type: ignore[arg-type]
                codes=codes,
                as_of=requested_dates[-1],
                lookback_days=(
                    365 + (requested_dates[-1] - requested_dates[0]).days
                ),
            )
        except Exception:  # noqa: BLE001
            logger.warning("读取规范化分红主数据失败", exc_info=True)
            all_dividend_events = []
        dividend_events_by_code: dict[str, list[object]] = {}
        for event in all_dividend_events:
            event_code = str(getattr(event, "code", ""))
            dividend_events_by_code.setdefault(event_code, []).append(event)

        for as_of in requested_dates:
            latest = self._valuation_latest(codes, as_of)
            for code in set(latest) | set(raw_by_date[as_of]):
                entry = latest.get(code, {})
                raw = raw_by_date[as_of].get(code, {})
                for field_name in ("pe_ttm", "pb"):
                    raw_value = raw.get(field_name)
                    legacy_value = entry[field_name][1] if field_name in entry else None
                    if (
                        raw_value is not None
                        and legacy_value is not None
                        and raw_value != 0
                    ):
                        difference = abs(raw_value - legacy_value) / abs(raw_value)
                        threshold = 0.02 if field_name != "pe_ttm" else 0.05
                        if difference > threshold:
                            source_mismatch_counts[field_name] = (
                                source_mismatch_counts.get(field_name, 0) + 1
                            )
                pe = raw.get("pe_ttm") or (
                    entry["pe_ttm"][1] if "pe_ttm" in entry else None
                )
                pb_value = raw.get("pb") or (entry["pb"][1] if "pb" in entry else None)
                ep = 1.0 / pe if pe is not None and pe > 0 else None
                bp = 1.0 / pb_value if pb_value is not None and pb_value > 0 else None
                from app.services.financial_statement_quality import (
                    market_cap_to_cny,
                )

                market_cap = (
                    market_cap_to_cny(raw["total_mv"])
                    if "total_mv" in raw
                    else market_cap_to_cny(entry["total_mv"][1])
                    if "total_mv" in entry
                    else None
                )
                float_market_cap = (
                    market_cap_to_cny(raw["circ_mv"])
                    if "circ_mv" in raw
                    else None
                )
                sales_yield = (
                    1.0 / raw["ps_ttm"]
                    if raw.get("ps_ttm") is not None and raw["ps_ttm"] > 0
                    else None
                )
                dividend_result = calculate_trailing_dividend_yield(
                    dividend_events_by_code.get(code, []),
                    code=code,
                    as_of=as_of,
                    price=prices_by_date[as_of].get(code),
                    source_covered=dividend_coverage.get(code, False),
                )
                dividend_yield = dividend_result.value
                # 规范化事件不可用时保留供应商直接计算值，但明确标记回退。
                if (
                    dividend_yield is None
                    and raw.get("dv_ttm") is not None
                ):
                    dividend_yield = raw["dv_ttm"] / 100.0
                    dividend_status = "provider_fallback"
                    dividend_reason = "规范化事件或价格缺失，回退 daily_basic.dv_ttm"
                else:
                    dividend_status = dividend_result.status
                    dividend_reason = dividend_result.reason
                if (
                    ep is None
                    and bp is None
                    and market_cap is None
                    and sales_yield is None
                    and dividend_yield is None
                ):
                    continue
                valuation_date = raw_trade_dates[as_of].get(code)
                if valuation_date is None and entry:
                    valuation_date = max(item[0] for item in entry.values())
                result.append(
                    Fundamentals(
                        code=code,
                        available_at=as_of,
                        period=None,
                        valuation_date=valuation_date,
                        ep=ep,
                        bp=bp,
                        market_cap=market_cap,
                        float_market_cap=float_market_cap,
                        sales_yield=sales_yield,
                        dividend_yield=dividend_yield,
                        dividend_yield_status=dividend_status,
                        dividend_yield_reason=dividend_reason,
                        dividend_event_count=dividend_result.event_count,
                        dividend_source_hashes=dividend_result.source_hashes,
                    )
                )
        if source_mismatch_counts:
            logger.warning(
                "估值跨源差异超过阈值（按字段主源 Tushare 取值）：%s",
                source_mismatch_counts,
            )
        return result

    def fundamentals(
        self,
        codes: list[str] | None = None,
        as_of: date | None = None,
    ) -> list[Fundamentals]:
        """PIT 财务快照（仅季度财务指标，不混入每日估值）。

        available_at 口径：披露日程的实际披露日优先，缺失回退财务行的
        available_at（入库时间近似）；两者皆无时按报告期法定最晚披露日
        保守估计（statutory_disclosure_deadline）并记 warning。
        每日估值由 valuation_snapshots 按实际信号日独立读取，避免将某个
        时点的 PE/PB 重复附着到所有历史财务报告。
        """
        from sqlalchemy import select

        from app.models import StockFinancialIndicator

        if codes is None:
            codes = [info.code for info in self.list_stocks(None)]
        if not codes:
            return []

        stmt = select(StockFinancialIndicator).where(
            StockFinancialIndicator.code.in_(codes)
        )
        rows = self._db.execute(stmt).scalars().all()  # type: ignore[attr-defined]
        disclosure = self._disclosure_map(codes)

        result: list[Fundamentals] = []
        estimated_count = 0
        for row in rows:
            available = disclosure.get((row.code, row.report_date)) or _as_date(
                row.available_at
            )
            if available is None:
                available = statutory_disclosure_deadline(row.report_date)
                estimated_count += 1
            if as_of is not None and available > as_of:
                continue
            roe = _to_float(row.roe)
            if roe is None:
                roe = _payload_value(row.payload, _ROE_KEYS)
            gross_margin = _payload_value(row.payload, _PAYLOAD_KEYS["gross_margin"])
            debt_ratio = _payload_value(row.payload, _PAYLOAD_KEYS["debt_ratio"])
            net_profit = _payload_value(row.payload, _PAYLOAD_KEYS["net_profit"])
            ocf = _payload_value(row.payload, _PAYLOAD_KEYS["ocf"])
            ocf_to_profit = _payload_value(row.payload, _PAYLOAD_KEYS["ocf_to_profit"])
            if (
                ocf_to_profit is None
                and ocf is not None
                and net_profit is not None
                and net_profit > 0
            ):
                ocf_to_profit = ocf / net_profit
            result.append(
                Fundamentals(
                    code=row.code,
                    available_at=available,
                    period=row.report_date,
                    roe=roe / 100.0 if roe is not None else None,
                    gross_margin=(
                        gross_margin / 100.0 if gross_margin is not None else None
                    ),
                    ocf_to_profit=ocf_to_profit,
                    debt_ratio=debt_ratio / 100.0 if debt_ratio is not None else None,
                )
            )

        if estimated_count:
            logger.warning(
                "%d 条财务快照缺失披露日/入库时间，available_at 按报告期法定"
                "最晚披露日保守估计（PIT 可能滞后于实际披露）",
                estimated_count,
            )

        # DuckDB fundamentals 数据集（长表 metric 口径）回退：仅当 ORM 无该行时
        if not result and self._warehouse_repo is not None:
            result.extend(self._fundamentals_from_warehouse(codes, as_of))
        # 已下载的三大报表与 fina_indicator 是字段最完整的 PIT 主源。
        # 同一股票/报告期若主源存在，删除回退源对应期，避免回退源较晚的
        # 入库时间覆盖真实公告日；同时对关键字段差异做显式质量告警。
        primary = self._fundamentals_from_tushare(codes, as_of)
        sector_metrics = self._financial_sector_metrics(codes, as_of)
        primary = [
            replace(
                snapshot,
                **sector_metrics.get(
                    (snapshot.code, snapshot.period),
                    {},
                ),
            )
            for snapshot in primary
        ]
        primary_periods = {
            (snapshot.code, snapshot.period)
            for snapshot in primary
            if snapshot.period is not None
        }
        fallback_by_period = {
            (snapshot.code, snapshot.period): snapshot
            for snapshot in result
            if snapshot.period is not None
        }
        mismatch_counts: dict[str, int] = {}
        for snapshot in primary:
            fallback = fallback_by_period.get((snapshot.code, snapshot.period))
            if fallback is None:
                continue
            for field_name, threshold in (("roe", 0.02),):
                selected = getattr(snapshot, field_name)
                other = getattr(fallback, field_name)
                if selected is None or other is None or selected == 0:
                    continue
                difference = abs(selected - other) / abs(selected)
                if difference > threshold:
                    mismatch_counts[field_name] = mismatch_counts.get(field_name, 0) + 1
        if mismatch_counts:
            logger.warning(
                "财务跨源差异超过阈值（按字段主源 Tushare 取值）：%s",
                mismatch_counts,
            )
        result = [
            snapshot
            for snapshot in result
            if (snapshot.code, snapshot.period) not in primary_periods
        ]
        result.extend(primary)
        result.sort(key=lambda snapshot: (snapshot.code, snapshot.available_at))
        return result

    def _financial_sector_metrics(
        self,
        codes: list[str],
        as_of: date | None,
    ) -> dict[tuple[str, date | None], dict[str, object]]:
        """读取监管/行业专用指标的不可变 PIT 规范记录。"""
        from sqlalchemy import select

        from app.models import QuantDataRecord

        allowed = {
            "bank_net_interest_margin",
            "bank_npl_ratio",
            "bank_provision_coverage_ratio",
            "bank_capital_adequacy_ratio",
            "bank_loan_deposit_ratio",
            "broker_proprietary_risk_ratio",
            "broker_leverage_ratio",
            "broker_net_capital_ratio",
            "insurance_solvency_ratio",
            "insurance_combined_ratio",
            "insurance_reserve_coverage_ratio",
        }
        statement = select(QuantDataRecord).where(
            QuantDataRecord.dataset == "financial_sector_metric",
            QuantDataRecord.code.in_(codes),
        )
        rows = self._db.scalars(statement).all()  # type: ignore[attr-defined]
        latest: dict[
            tuple[str, date], tuple[datetime, int, dict[str, object]]
        ] = {}
        for row in rows:
            available = row.available_at
            available_day = (
                available.date() if isinstance(available, datetime) else available
            )
            if as_of is not None and available_day > as_of:
                continue
            payload = dict(row.payload or {})
            key = (row.code, row.effective_date)
            candidate = (
                available,
                row.id,
                {
                    **{
                        field: _to_float(payload.get(field))
                        for field in allowed
                        if payload.get(field) is not None
                    },
                    "sector_metric_sources": (row.source_hash,),
                },
            )
            previous = latest.get(key)
            if previous is None or candidate[:2] > previous[:2]:
                latest[key] = candidate
        return {key: value[2] for key, value in latest.items()}

    def _fundamentals_from_tushare(
        self, codes: list[str], as_of: date | None
    ) -> list[Fundamentals]:
        """把三大报表按公告日保守合并为 PIT 财务快照。

        同一报告期仅在利润表、资产负债表、现金流和指标表各自最新可得
        版本中取值；available_at 取参与字段公告日的最大值，确保不会提前
        使用尚未公开的报表。
        """
        try:
            import pyarrow.parquet as pq
        except Exception:  # noqa: BLE001
            return []

        datasets: dict[str, tuple[str, ...]] = {
            "income": (
                "ann_date",
                "f_ann_date",
                "end_date",
                "report_type",
                "comp_type",
                "end_type",
                "update_flag",
                "total_revenue",
                "revenue",
                "n_income_attr_p",
                "n_income",
            ),
            "balancesheet": (
                "ann_date",
                "f_ann_date",
                "end_date",
                "report_type",
                "comp_type",
                "end_type",
                "update_flag",
                "total_assets",
                "total_liab",
                "total_hldr_eqy_exc_min_int",
                "total_hldr_eqy_inc_min_int",
                "total_liab_hldr_eqy",
            ),
            "cashflow": (
                "ann_date",
                "f_ann_date",
                "end_date",
                "report_type",
                "comp_type",
                "end_type",
                "update_flag",
                "net_profit",
                "n_cashflow_act",
                "c_pay_acq_const_fiolta",
                "n_incr_cash_cash_equ",
                "c_cash_equ_beg_period",
                "c_cash_equ_end_period",
                "free_cashflow",
            ),
            "fina_indicator": (
                "ann_date",
                "end_date",
                "roe",
                "roa",
                "roa_dp",
                "gross_margin",
                "grossprofit_margin",
                "netprofit_margin",
                "debt_to_assets",
            ),
        }
        snapshots: list[Fundamentals] = []
        for code in codes:
            by_period: dict[date, dict[str, object]] = {}
            available_by_period: dict[date, list[date]] = {}
            for dataset, columns in datasets.items():
                path = self._stocktoday_file(dataset, code)
                if path is None:
                    continue
                try:
                    table = pq.read_table(path, columns=list(columns))
                except Exception:  # noqa: BLE001
                    logger.warning("财务原始表损坏：%s", path, exc_info=True)
                    continue
                # 同一报告期可能有更正，按公告日保留当时最新公开版本。
                latest: dict[date, tuple[date, dict[str, object]]] = {}
                for row in table.to_pylist():
                    period = _to_compact_date(row.get("end_date"))
                    available = _to_compact_date(
                        row.get("f_ann_date") or row.get("ann_date")
                    )
                    if period is None or available is None:
                        continue
                    if as_of is not None and available > as_of:
                        continue
                    current = latest.get(period)
                    if current is None or available >= current[0]:
                        latest[period] = (available, row)
                for period, (available, row) in latest.items():
                    combined = by_period.setdefault(period, {})
                    statement_rows = combined.setdefault(
                        "_statement_rows", {}
                    )
                    statement_rows[dataset] = dict(row)  # type: ignore[index]
                    combined.update(row)
                    available_by_period.setdefault(period, []).append(available)
            for period, row in by_period.items():
                availability = available_by_period.get(period, [])
                if not availability:
                    continue
                available = max(availability)
                roe = _to_float(row.get("roe"))
                roa = _to_float(row.get("roa")) or _to_float(row.get("roa_dp"))
                gross_margin = _to_float(row.get("gross_margin"))
                if gross_margin is None:
                    gross_margin = _to_float(row.get("grossprofit_margin"))
                net_margin = _to_float(row.get("netprofit_margin"))
                debt_ratio = _to_float(row.get("debt_to_assets"))
                total_assets = _to_float(row.get("total_assets"))
                total_liab = _to_float(row.get("total_liab"))
                if (
                    debt_ratio is None
                    and total_assets is not None
                    and total_assets > 0
                    and total_liab is not None
                ):
                    debt_ratio = total_liab / total_assets
                net_income = _to_float(row.get("n_income_attr_p"))
                if net_income is None:
                    net_income = _to_float(row.get("n_income"))
                if net_income is None:
                    net_income = _to_float(row.get("net_profit"))
                ocf = _to_float(row.get("n_cashflow_act"))
                ocf_to_profit = (
                    ocf / net_income
                    if ocf is not None and net_income is not None and net_income > 0
                    else None
                )
                from app.services.financial_statement_quality import (
                    assess_statement_bundle,
                )

                assessment = assess_statement_bundle(
                    period=period,
                    rows=dict(row.get("_statement_rows") or {}),
                )
                snapshots.append(
                    Fundamentals(
                        code=code,
                        available_at=available,
                        period=period,
                        roe=roe / 100.0 if roe is not None else None,
                        roa=roa / 100.0 if roa is not None else None,
                        gross_margin=(
                            gross_margin / 100.0 if gross_margin is not None else None
                        ),
                        net_margin=(
                            net_margin / 100.0 if net_margin is not None else None
                        ),
                        ocf_to_profit=ocf_to_profit,
                        debt_ratio=(
                            debt_ratio / 100.0
                            if debt_ratio is not None and debt_ratio > 1.5
                            else debt_ratio
                        ),
                        revenue=_to_float(
                            row.get("total_revenue") or row.get("revenue")
                        ),
                        net_income=net_income,
                        operating_cash_flow=ocf,
                        capital_expenditure=_to_float(
                            row.get("c_pay_acq_const_fiolta")
                        ),
                        free_cash_flow=_to_float(row.get("free_cashflow")),
                        total_assets=total_assets,
                        total_equity=_to_float(row.get("total_hldr_eqy_exc_min_int")),
                        company_type=(
                            str(row.get("comp_type"))
                            if row.get("comp_type") is not None
                            else None
                        ),
                        formal_factor_usable=assessment.formal_factor_usable,
                        financial_quality_reasons=(
                            assessment.errors + assessment.warnings
                        ),
                        unit_policy=assessment.unit_policy,
                        flow_basis=assessment.flow_basis,
                        audit_opinion=assessment.audit_opinion,
                        correction_status=assessment.correction_status,
                    )
                )
        from app.services.financial_ttm import build_pit_ttm

        return build_pit_ttm(snapshots)

    def _fundamentals_from_warehouse(
        self, codes: list[str], as_of: date | None
    ) -> list[Fundamentals]:
        try:
            frame = self._warehouse_repo.read_fundamentals(  # type: ignore[attr-defined]
                codes, as_of=datetime.combine(as_of, time.max) if as_of else None
            )
        except Exception:  # noqa: BLE001
            return []
        if frame is None or frame.empty:
            return []
        grouped: dict[tuple[str, date], dict[str, float]] = {}
        periods: dict[tuple[str, date], date | None] = {}
        for record in frame.to_dict(orient="records"):
            available = _as_date(record.get("available_at")) or _as_date(
                record.get("effective_date")
            )
            code = record.get("symbol")
            metric = str(record.get("metric") or "")
            value = _to_float(record.get("metric_value"))
            if code is None or available is None or value is None:
                continue
            key = (str(code), available)
            for field, names in _METRIC_KEYS.items():
                if metric in names:
                    grouped.setdefault(key, {})[field] = value
            periods[key] = _as_date(record.get("effective_date"))
        result: list[Fundamentals] = []
        for (code, available), fields in grouped.items():
            result.append(
                Fundamentals(
                    code=code,
                    available_at=available,
                    period=periods.get((code, available)),
                    roe=(
                        fields["roe"] / 100.0
                        if fields.get("roe") is not None and fields["roe"] > 1.5
                        else fields.get("roe")
                    ),
                    gross_margin=(
                        fields["gross_margin"] / 100.0
                        if fields.get("gross_margin") is not None
                        and fields["gross_margin"] > 1.5
                        else fields.get("gross_margin")
                    ),
                    ocf_to_profit=fields.get("ocf_to_profit"),
                    debt_ratio=(
                        fields["debt_ratio"] / 100.0
                        if fields.get("debt_ratio") is not None
                        and fields["debt_ratio"] > 1.5
                        else fields.get("debt_ratio")
                    ),
                    ep=fields.get("ep"),
                    bp=fields.get("bp"),
                )
            )
        return result

    # ---- 指数基准 ----------------------------------------------------------

    def _verify_governed_files(
        self, source_files: tuple[str, ...], source_hashes: tuple[str, ...]
    ) -> None:
        from app.config import get_settings

        root = self._root or Path(get_settings().research_data_dir)
        file_hashes: set[str] = set()
        for relative in source_files:
            path = root / relative
            if not path.is_file():
                raise ValueError(f"受治理源文件不存在：{relative}")
            self._record_file(path)
            file_hashes.add(hashlib.sha256(path.read_bytes()).hexdigest())
        if file_hashes != set(source_hashes):
            raise ValueError("受治理源文件哈希与规范化记录不一致")

    def index_weight_snapshot(
        self, index_code: str, as_of: date
    ) -> IndexWeightSnapshot:
        """返回最新官方权重截面；缺失、权重和异常或源文件损坏均硬失败。"""
        from sqlalchemy import func, select

        from app.models import QuantDataRecord

        prefix = f"{index_code}:%"
        snapshot_date = self._db.scalar(  # type: ignore[attr-defined]
            select(func.max(QuantDataRecord.effective_date)).where(
                QuantDataRecord.dataset == "index_weight",
                QuantDataRecord.code.like(prefix),
                QuantDataRecord.effective_date <= as_of,
                QuantDataRecord.available_at <= datetime.combine(as_of, time.max),
            )
        )
        if snapshot_date is None:
            raise ValueError(f"{index_code} 在 {as_of} 没有可用官方权重")
        rows = list(
            self._db.scalars(  # type: ignore[attr-defined]
                select(QuantDataRecord)
                .where(
                    QuantDataRecord.dataset == "index_weight",
                    QuantDataRecord.code.like(prefix),
                    QuantDataRecord.effective_date == snapshot_date,
                )
                .order_by(QuantDataRecord.code, QuantDataRecord.imported_at)
            ).all()
        )
        latest = {row.code: row for row in rows}
        weights: list[tuple[str, float]] = []
        for row in latest.values():
            payload = dict(row.payload or {})
            weight = _to_float(payload.get("weight_percent"))
            stock_code = str(payload.get("stock_code") or "")
            if not stock_code or weight is None or weight <= 0:
                raise ValueError(f"{index_code} {snapshot_date} 存在无效成分权重")
            weights.append((stock_code, weight))
        expected_minimum = 280 if index_code == "000300" else 450
        if len(weights) < expected_minimum:
            raise ValueError(
                f"{index_code} {snapshot_date} 仅 {len(weights)} 只成分，"
                f"低于完整性门槛 {expected_minimum}"
            )
        weight_sum = sum(weight for _code, weight in weights)
        if not 99.5 <= weight_sum <= 100.5:
            raise ValueError(
                f"{index_code} {snapshot_date} 权重和 {weight_sum:.6f}% 异常"
            )
        source_files = tuple(sorted({row.source_file for row in latest.values()}))
        source_hashes = tuple(sorted({row.source_hash for row in latest.values()}))
        self._verify_governed_files(source_files, source_hashes)
        return IndexWeightSnapshot(
            index_code=index_code,
            as_of=as_of,
            snapshot_date=snapshot_date,
            weights=tuple(sorted(weights)),
            weight_sum_percent=weight_sum,
            source_files=source_files,
            source_hashes=source_hashes,
        )

    def combined_csi800_weights(self, as_of: date) -> CombinedIndexWeights:
        """用固定的 50% 沪深300 + 50% 中证500袖套合成透明比较权重。"""
        components = tuple(
            self.index_weight_snapshot(index_code, as_of)
            for index_code in ("000300", "000905")
        )
        combined: dict[str, float] = {}
        for snapshot in components:
            normalizer = snapshot.weight_sum_percent
            for code, weight_percent in snapshot.weights:
                combined[code] = combined.get(code, 0.0) + (
                    0.5 * weight_percent / normalizer
                )
        if abs(sum(combined.values()) - 1.0) > 1e-10:
            raise ValueError("300+500 合成权重未归一到 100%")
        return CombinedIndexWeights(
            as_of=as_of,
            method="equal_sleeve_50_50_official_weights_v1",
            weights=tuple(sorted(combined.items())),
            component_snapshots=components,
        )

    def benchmark_series(
        self,
        index_code: str,
        start: date | None = None,
        end: date | None = None,
    ) -> BenchmarkSeries | None:
        """读取规范化官方基准并逐个校验版本化源文件哈希。"""
        try:
            from sqlalchemy import select

            from app.models import QuantDataRecord

            statement = select(QuantDataRecord).where(
                QuantDataRecord.dataset == "index_total_return",
                QuantDataRecord.code == index_code,
            )
            if start is not None:
                statement = statement.where(QuantDataRecord.effective_date >= start)
            if end is not None:
                statement = statement.where(QuantDataRecord.effective_date <= end)
            rows = list(
                self._db.scalars(  # type: ignore[attr-defined]
                    statement.order_by(
                        QuantDataRecord.effective_date,
                        QuantDataRecord.imported_at,
                    )
                ).all()
            )
            if not rows:
                return None
            latest = {row.effective_date: row for row in rows}
            selected = [latest[day] for day in sorted(latest)]
            source_files = tuple(sorted({row.source_file for row in selected}))
            source_hashes = tuple(sorted({row.source_hash for row in selected}))
            self._verify_governed_files(source_files, source_hashes)
            points: list[tuple[date, float]] = []
            for row in selected:
                close = _to_float(dict(row.payload or {}).get("close"))
                if close is None or close <= 0:
                    raise ValueError(
                        f"官方基准 {index_code} {row.effective_date} 点位无效"
                    )
                points.append((row.effective_date, close))
            payload = dict(selected[-1].payload or {})
            return BenchmarkSeries(
                code=index_code,
                name=str(payload.get("index_name") or index_code),
                return_kind=str(payload.get("return_kind") or "unknown"),
                points=tuple(points),
                source=str(selected[-1].source),
                source_files=source_files,
                source_hashes=source_hashes,
            )
        except Exception:  # noqa: BLE001 - 正式调用方会按 required 硬失败
            logger.warning("读取受治理官方基准失败", exc_info=True)
            return None

    def index_bars(
        self,
        index_code: str,
        start: date | None = None,
        end: date | None = None,
    ) -> list[tuple[date, float]]:
        """指数收盘序列（MarketIndex/IndexQuote，字段名防御式探测）。"""
        try:
            module = importlib.import_module("app.models")
            index_model = getattr(module, "MarketIndex", None)
            quote_model = getattr(module, "IndexQuote", None)
            if index_model is None or quote_model is None:
                return []
            from sqlalchemy import select

            index = self._db.scalar(  # type: ignore[attr-defined]
                select(index_model).where(getattr(index_model, "code") == index_code)
            )
            if index is None:
                return []
            date_col = next(
                (
                    getattr(quote_model, c)
                    for c in ("quote_date", "trade_date", "nav_date", "date")
                    if hasattr(quote_model, c)
                ),
                None,
            )
            value_col = next(
                (
                    getattr(quote_model, c)
                    for c in ("close", "close_price", "price", "value")
                    if hasattr(quote_model, c)
                ),
                None,
            )
            fk_col = next(
                (
                    getattr(quote_model, c)
                    for c in ("index_id", "market_index_id")
                    if hasattr(quote_model, c)
                ),
                None,
            )
            if date_col is None or value_col is None or fk_col is None:
                return []
            rows = self._db.execute(  # type: ignore[attr-defined]
                select(date_col, value_col).where(fk_col == index.id).order_by(date_col)
            ).all()
            series: list[tuple[date, float]] = []
            for day, value in rows:
                parsed = _as_date(day)
                number = _to_float(value)
                if parsed is None or number is None or number <= 0:
                    continue
                if start is not None and parsed < start:
                    continue
                if end is not None and parsed > end:
                    continue
                series.append((parsed, number))
            return series
        except Exception:  # noqa: BLE001 - 指数缺失按无基准降级
            return []


# ---------------------------------------------------------------------------
# 动态装载：显式注入 > 注册工厂 > 约定模块探测 > 内置 SQL 适配器
# ---------------------------------------------------------------------------

_FACTORY: Callable[[object], StockRepository] | None = None


def register_repository_factory(factory: Callable[[object], StockRepository]) -> None:
    """注册仓储工厂（供未来的 stock data 模块在导入时挂钩）。"""
    global _FACTORY
    _FACTORY = factory


def reset_repository_factory() -> None:
    """清除已注册工厂（测试隔离用）。"""
    global _FACTORY
    _FACTORY = None


def _looks_like_repository(repo: object) -> bool:
    """duck-typing 校验：核心方法齐备即视为可用仓储。"""
    return all(
        callable(getattr(repo, name, None))
        for name in ("list_stocks", "daily_bars", "fundamentals", "trade_calendar")
    )


def _try_future_module(db: object) -> StockRepository | None:
    """探测约定模块 app.services.stock_repository.get_repository(db)。

    self-probe：本模块自身即该名称，未来若要整体替换仓储实现，
    在本模块追加 get_repository 函数即可（当前不存在，探测必然落空）。
    """
    module = importlib.import_module("app.services.stock_repository")
    factory = getattr(module, "get_repository", None)
    if factory is None:
        return None
    try:
        repo = factory(db)
    except Exception:  # noqa: BLE001 - 探测失败降级，不影响主流程
        return None
    return repo if _looks_like_repository(repo) else None


def load_repository(
    db: object = None, repository: StockRepository | None = None
) -> StockRepository | None:
    """按优先级装载仓储；db 为 None 且未注入时返回 None。"""
    if repository is not None:
        return repository
    if _FACTORY is not None:
        try:
            repo = _FACTORY(db)
        except Exception:  # noqa: BLE001 - 工厂失败继续向下探测
            repo = None
        if repo is not None and _looks_like_repository(repo):
            return repo
    repo = _try_future_module(db)
    if repo is not None:
        return repo
    if db is not None:
        return SqlStockRepository(db)
    return None


__all__ = [
    "Fundamentals",
    "MarketBars",
    "NamePeriod",
    "SqlStockRepository",
    "StockBar",
    "StockInfo",
    "StockRepository",
    "TradeCalendar",
    "board_of",
    "coerce_bar",
    "coerce_fundamentals",
    "coerce_info",
    "is_st_name",
    "load_repository",
    "one_word_limit",
    "price_limit_for",
    "register_repository_factory",
    "reset_repository_factory",
    "st_status_as_of",
    "statutory_disclosure_deadline",
]
