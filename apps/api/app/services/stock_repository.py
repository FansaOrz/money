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
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, time
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
    roe: float | None = None
    gross_margin: float | None = None
    ocf_to_profit: float | None = None
    debt_ratio: float | None = None
    ep: float | None = None
    bp: float | None = None


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
    current_name: str, periods: list[NamePeriod] | tuple[NamePeriod, ...] | None, as_of: date
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

    # ---- 可选扩展（duck-typed，缺失时服务层自动降级）----------------------
    # def market_bars(self, codes, start=None, end=None) -> dict[str, MarketBars]:
    #     研究(qfq)/执行(raw) 双口径面板；缺失时引擎退化为 raw 单口径。
    # def name_histories(self, codes) -> dict[str, list[NamePeriod]]:
    #     历史名称/ST 区间；缺失时按当前名称判定 ST。


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
        return float(value)  # type: ignore[arg-type]
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
        ocf_to_profit=_to_float(_attr(obj, "ocf_to_profit", "ocf_to_np", "cash_to_profit")),
        debt_ratio=_to_float(_attr(obj, "debt_ratio", "debt_to_assets", "leverage")),
        ep=_to_float(_attr(obj, "ep", "earnings_yield", "e_p")),
        bp=_to_float(_attr(obj, "bp", "book_to_price", "b_p")),
    )


# ---------------------------------------------------------------------------
# 内置 SQL 适配器：现有数据层（ORM + Parquet 数据湖 + DuckDB 研究仓库）
# ---------------------------------------------------------------------------

# 财务 payload（新浪原始 JSON）中候选中文字段名 → 归一化字段
_PAYLOAD_KEYS: dict[str, tuple[str, ...]] = {
    "gross_margin": (
        "销售毛利率(%)", "销售毛利率", "gross_margin", "grossprofit_margin",
        "XSMLL", "sale_gross_margin",
    ),
    "debt_ratio": (
        "资产负债率(%)", "资产负债率", "debt_ratio", "debt_to_assets",
        "ZCFZL", "assets_debt_ratio",
    ),
    "net_profit": (
        "净利润(元)", "净利润", "归属于母公司所有者的净利润(元)",
        "归属于母公司所有者的净利润", "net_profit", "PARENTNETPROFIT",
    ),
    "ocf": (
        "经营活动产生的现金流量净额(元)", "经营活动产生的现金流量净额", "ocf",
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
_INDUSTRY_MODELS = ("StockIndustry", "StockIndustryMember", "StockIndustryClassification")

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
        self._warehouse_repo = self._try_warehouse()
        self._industry_map = self._load_industries()

    # ---- 可选数据源探测 -------------------------------------------------

    def _try_warehouse(self) -> object | None:
        """DuckDB 研究仓库存在时以只读方式接入；任何失败返回 None。"""
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
            (getattr(model, c) for c in ("code", "stock_code", "symbol") if hasattr(model, c)),
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
            return {
                code: industry for code, (_score, industry) in selected.items()
            }
        except Exception:  # noqa: BLE001 - 表结构不符预期时降级
            return {}

    # ---- 清单 ------------------------------------------------------------

    def list_stocks(self, codes: list[str] | None = None) -> list[StockInfo]:
        from sqlalchemy import select

        from app.models import StockMaster

        stmt = select(StockMaster).order_by(StockMaster.code)
        if codes:
            stmt = stmt.where(StockMaster.code.in_(codes))
        result: list[StockInfo] = []
        for row in self._db.execute(stmt).scalars().all():  # type: ignore[attr-defined]
            result.append(
                StockInfo(
                    code=row.code,
                    name=row.name,
                    industry=self._industry_map.get(row.code, "未知"),
                    list_date=None,  # master 无上市日期：按行情首日近似（策略层兜底）
                )
            )
        return result

    # ---- 日线行情 ---------------------------------------------------------

    def _bars_from_parquet(
        self, code: str, start: date | None, end: date | None, layer: str | None = None
    ) -> list[StockBar]:
        try:
            from app.services.research import parquet_store

            frame = parquet_store.read_daily(
                code, layer or parquet_store.DAILY_RAW, self._root
            )
        except Exception:  # noqa: BLE001 - 数据湖读取失败按无数据处理
            return []
        if frame is None:
            return []
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
            suspended = (
                (volume is None or volume <= _SUSPEND_EPS)
                and (amount is None or amount <= _SUSPEND_EPS)
            )
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
                )
            )
        return bars

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
            exec_bars = sorted(
                exec_map.get(code, []), key=lambda bar: bar.trade_date
            )
            research_bars = self._bars_from_parquet(
                code, start, end, layer=parquet_store.DAILY_QFQ
            )
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
        """交易日历：行情日期的并集（数据湖断点表范围内抽样）。"""
        from sqlalchemy import select

        from app.models import StockDailyBar

        codes = list(self._db.execute(select(StockDailyBar.code)).scalars().all())  # type: ignore[attr-defined]
        if not codes:
            codes = [info.code for info in self.list_stocks(None)]
        days: set[date] = set()
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
            StockValuation.indicator.in_(("pe_ttm", "pb")),
        )
        if as_of is not None:
            latest_dates = latest_dates.where(
                StockValuation.trade_date <= as_of
            )
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

    def fundamentals(
        self,
        codes: list[str] | None = None,
        as_of: date | None = None,
    ) -> list[Fundamentals]:
        """PIT 财务快照：财务指标（披露日 PIT）+ 估值（EP/BP）。

        available_at 口径：披露日程的实际披露日优先，缺失回退财务行的
        available_at（入库时间近似）；两者皆无时按报告期法定最晚披露日
        保守估计（statutory_disclosure_deadline）并记 warning。
        估值（EP/BP）按 as_of 取 trade_date ≤ as_of 的最新一条，无未来数据。
        """
        from sqlalchemy import select

        from app.models import StockFinancialIndicator

        if codes is None:
            codes = [info.code for info in self.list_stocks(None)]
        if not codes:
            return []

        stmt = select(StockFinancialIndicator).where(StockFinancialIndicator.code.in_(codes))
        rows = self._db.execute(stmt).scalars().all()  # type: ignore[attr-defined]
        disclosure = self._disclosure_map(codes)
        valuations = self._valuation_latest(codes, as_of)

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
            ocf_to_profit = _payload_value(
                row.payload, _PAYLOAD_KEYS["ocf_to_profit"]
            )
            if (
                ocf_to_profit is None
                and ocf is not None
                and net_profit is not None
                and net_profit > 0
            ):
                ocf_to_profit = ocf / net_profit
            entry = valuations.get(row.code, {})
            ep = 1.0 / entry["pe_ttm"][1] if "pe_ttm" in entry else None
            bp = 1.0 / entry["pb"][1] if "pb" in entry else None
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
                    ep=ep,
                    bp=bp,
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
        return result

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
