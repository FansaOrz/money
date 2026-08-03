"""AKShare 接口薄封装。

集中管理外部数据调用点，统一约定：
- 任何网络/限流/接口变更异常都记日志并返回 None（优雅降级）；
- 未安装 akshare 时同样返回 None；
- 测试中 monkeypatch 本模块函数即可完全离线。
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

import pandas as pd

logger = logging.getLogger(__name__)


def _call(fn_name: str, *args: Any, **kwargs: Any) -> pd.DataFrame | None:
    """调用 akshare.<fn_name>，失败返回 None。"""
    try:
        import akshare as ak
    except ImportError:
        logger.warning("未安装 akshare，无法调用 %s", fn_name)
        return None
    fn: Callable[..., Any] | None = getattr(ak, fn_name, None)
    if fn is None:
        logger.warning("akshare 缺少接口 %s", fn_name)
        return None
    try:
        frame = fn(*args, **kwargs)
    except Exception as exc:  # 网络超时/限流/接口字段变更等，统一降级
        logger.warning("akshare %s 调用失败：%s", fn_name, exc)
        return None
    if frame is None:
        return None
    if not isinstance(frame, pd.DataFrame):
        logger.warning("akshare %s 返回非 DataFrame：%s", fn_name, type(frame))
        return None
    return frame if not frame.empty else None


def fetch_stock_code_name() -> pd.DataFrame | None:
    """A 股代码/名称主表（ak.stock_info_a_code_name）。列：code, name。"""
    return _call("stock_info_a_code_name")


def fetch_index_cons(index_code: str) -> pd.DataFrame | None:
    """指数当前成分（ak.index_stock_cons_csindex）。symbol 为 6 位指数代码。"""
    return _call("index_stock_cons_csindex", symbol=index_code)


def fetch_stock_daily_sina(symbol: str, adjust: str = "") -> pd.DataFrame | None:
    """新浪 A 股日线（ak.stock_zh_a_daily）。

    symbol 形如 sh600000；adjust="" 为不复权（raw），"qfq" 为前复权。
    返回列（新浪源）：date/open/high/low/close/volume/amount/outstanding_share/turnover。
    """
    return _call("stock_zh_a_daily", symbol=symbol, adjust=adjust)


def fetch_financial_indicator(symbol: str) -> pd.DataFrame | None:
    """财务分析指标（ak.stock_financial_analysis_indicator，新浪）。symbol 为 6 位代码。"""
    return _call("stock_financial_analysis_indicator", symbol=symbol)


def fetch_report_disclosure(market: str, period: str) -> pd.DataFrame | None:
    """全市场财报披露日程（ak.stock_report_disclosure，巨潮资讯）。

    当前 akshare 版本该接口为全市场快照：一次请求返回指定 market+period
    下全部股票的披露日程，无需（也无法）按个股查询。

    market: "沪深京"（全市场）/"沪市"/"深市"/"北交所" 等；
    period: 形如 "2024年报" / "2025一季" / "2025半年报" / "2025三季"。

    返回列：股票代码/股票简称/首次预约/初次变更/二次变更/三次变更/实际披露。
    """
    return _call("stock_report_disclosure", market=market, period=period)


def fetch_valuation_baidu(symbol: str, indicator: str, period: str = "近一年") -> pd.DataFrame | None:
    """百度股市通估值（ak.stock_zh_valuation_baidu）。

    indicator: 总市值/市盈率(TTM)/市净率/市销率(TTM)/股息率
    period: 近一年/近三年/近五年/近十年/全部
    """
    return _call("stock_zh_valuation_baidu", symbol=symbol, indicator=indicator, period=period)


def fetch_industry_boards() -> pd.DataFrame | None:
    """东方财富行业板块列表（ak.stock_board_industry_name_em）。列：板块名称 等。"""
    return _call("stock_board_industry_name_em")


def fetch_industry_cons(board_name: str) -> pd.DataFrame | None:
    """东方财富行业板块成分（ak.stock_board_industry_cons_em）。列：代码/名称 等。"""
    return _call("stock_board_industry_cons_em", symbol=board_name)


def fetch_industry_change_cninfo(symbol: str) -> pd.DataFrame | None:
    """巨潮上市公司行业归属变动（回退源，覆盖成立以来至今天）。"""
    from datetime import date

    return _call(
        "stock_industry_change_cninfo",
        symbol=symbol,
        start_date="19900101",
        end_date=date.today().strftime("%Y%m%d"),
    )


def fetch_name_change_hist(symbol: str) -> pd.DataFrame | None:
    """历史名称/曾用名（ak.stock_info_change_name，新浪公司资料页）。

    symbol 为 6 位代码。返回行序号 index（旧->新，1 起）与 name（含曾用名
    区间标记如 "中科健A->ST科健->..."）；接口不带日期，区间由解析方推导。
    注意：ak.stock_name_change_hist 在当前 akshare 版本已移除。
    """
    return _call("stock_info_change_name", symbol=symbol)
