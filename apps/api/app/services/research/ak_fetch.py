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


def fetch_stock_spot_eastmoney() -> pd.DataFrame | None:
    """东方财富全市场 A 股实时快照；收盘后作为当日日线快速通道。"""
    return _call("stock_zh_a_spot_em")


def fetch_stock_spot_sina() -> pd.DataFrame | None:
    """新浪全市场 A 股实时快照；作为东方财富收盘快照的回退源。"""
    return _call("stock_zh_a_spot")


def fetch_stock_spot_tencent() -> pd.DataFrame | None:
    """腾讯全市场 A 股实时快照；提供 PE(TTM)、市值等估值字段。"""
    return _call("stock_zh_a_spot_tx")


def fetch_trade_calendar_sina() -> pd.DataFrame | None:
    """沪深交易日历（新浪）；用于阻止周末/节假日快照伪装成日线。"""
    return _call("tool_trade_date_hist_sina")


def fetch_index_cons(index_code: str) -> pd.DataFrame | None:
    """指数当前成分（ak.index_stock_cons_csindex）。symbol 为 6 位指数代码。"""
    return _call("index_stock_cons_csindex", symbol=index_code)


def fetch_stock_daily_sina(symbol: str, adjust: str = "") -> pd.DataFrame | None:
    """新浪 A 股日线（ak.stock_zh_a_daily）。

    symbol 形如 sh600000；adjust="" 为不复权（raw），"qfq" 为前复权。
    返回列（新浪源）：date/open/high/low/close/volume/amount/outstanding_share/turnover。
    """
    return _call("stock_zh_a_daily", symbol=symbol, adjust=adjust)


def fetch_stock_daily_eastmoney(
    symbol: str,
    *,
    start_date: str = "19900101",
    end_date: str = "20991231",
    adjust: str = "",
) -> pd.DataFrame | None:
    """东方财富 A 股历史日线回退源（ak.stock_zh_a_hist）。

    symbol 为 6 位代码；period 固定 daily。输出通常为中文列：
    日期/股票代码/开盘/收盘/最高/最低/成交量/成交额/换手率。
    """
    return _call(
        "stock_zh_a_hist",
        symbol=symbol,
        period="daily",
        start_date=start_date,
        end_date=end_date,
        adjust=adjust,
    )


def fetch_stock_daily_tencent(
    symbol: str,
    *,
    start_date: str = "19900101",
    end_date: str = "20991231",
    adjust: str = "",
) -> pd.DataFrame | None:
    """腾讯 A 股历史日线第二回退源，兼容 689009 等 CDR 标的。"""
    return _call(
        "stock_zh_a_hist_tx",
        symbol=symbol,
        start_date=start_date,
        end_date=end_date,
        adjust=adjust,
        timeout=20,
    )


def fetch_financial_indicator(symbol: str) -> pd.DataFrame | None:
    """财务分析指标（ak.stock_financial_analysis_indicator，新浪）。symbol 为 6 位代码。"""
    return _call("stock_financial_analysis_indicator", symbol=symbol)


def fetch_financial_indicator_eastmoney(symbol: str) -> pd.DataFrame | None:
    """东方财富财务分析主要指标；symbol 形如 600519.SH / 000001.SZ。"""
    return _call(
        "stock_financial_analysis_indicator_em",
        symbol=symbol,
        indicator="按报告期",
    )


def fetch_financial_indicator_ths(symbol: str) -> pd.DataFrame | None:
    """同花顺财务重要指标，作为东方财富和新浪均失败时的第三数据源。"""
    return _call(
        "stock_financial_abstract_new_ths",
        symbol=symbol,
        indicator="按报告期",
    )


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
    """百度股市通估值。

    indicator: 总市值/市盈率(TTM)/市净率/市销率(TTM)/股息率
    period: 近一年/近三年/近五年/近十年/全部
    直接调用公开接口，为 AKShare 原实现补上连接和读取超时。
    """
    try:
        import requests

        response = requests.get(
            "https://gushitong.baidu.com/opendata",
            params={
                "openapi": "1",
                "dspName": "iphone",
                "tn": "tangram",
                "client": "app",
                "query": indicator,
                "code": symbol,
                "word": "",
                "resource_id": "51171",
                "market": "ab",
                "tag": indicator,
                "chart_select": period,
                "industry_select": "",
                "skip_industry": "1",
                "finClientType": "pc",
            },
            timeout=(5, 15),
        )
        response.raise_for_status()
        body = response.json()["Result"][0]["DisplayData"]["resultData"]["tplData"][
            "result"
        ]["chartInfo"][0]["body"]
        frame = pd.DataFrame(body, columns=["date", "value"])
        frame["date"] = pd.to_datetime(frame["date"], errors="coerce").dt.date
        frame["value"] = pd.to_numeric(frame["value"], errors="coerce")
        return frame if not frame.empty else None
    except Exception as exc:
        logger.warning(
            "baidu 估值调用失败（%s/%s）：%s", symbol, indicator, exc
        )
        return None


def fetch_industry_boards() -> pd.DataFrame | None:
    """东方财富行业板块列表（ak.stock_board_industry_name_em）。列：板块名称 等。"""
    return _call("stock_board_industry_name_em")


def fetch_industry_cons(board_name: str) -> pd.DataFrame | None:
    """东方财富行业板块成分（ak.stock_board_industry_cons_em）。列：代码/名称 等。"""
    return _call("stock_board_industry_cons_em", symbol=board_name)


def fetch_industry_change_cninfo(symbol: str) -> pd.DataFrame | None:
    """巨潮上市公司行业归属变动（回退源，覆盖成立以来至今天）。

    AKShare 对应函数未设置 HTTP 超时，批量维护时单个坏连接会永久阻塞。
    此处直接调用同一公开接口并设置连接/读取硬超时。
    """
    from datetime import date

    try:
        import requests
        from akshare.stock.stock_industry_cninfo import _get_file_content_ths
        from py_mini_racer import py_mini_racer

        js = py_mini_racer.MiniRacer()
        js.eval(_get_file_content_ths("cninfo.js"))
        headers = _cninfo_headers(js.call("getResCode1"))
        end_date = date.today().strftime("%Y-%m-%d")
        response = requests.post(
            "https://webapi.cninfo.com.cn/api/stock/p_stock2110",
            params={"scode": symbol, "sdate": "1990-01-01", "edate": end_date},
            headers=headers,
            timeout=(5, 15),
        )
        response.raise_for_status()
        frame = pd.DataFrame(response.json().get("records") or [])
        if frame.empty:
            return None
        frame.rename(
            columns={
                "VARYDATE": "变更日期",
                "F004V": "行业门类",
                "F006V": "行业大类",
                "F007V": "行业中类",
            },
            inplace=True,
        )
        return frame
    except Exception as exc:
        logger.warning("cninfo 行业变更调用失败（%s）：%s", symbol, exc)
        return None


def fetch_stock_profile_cninfo(symbol: str) -> pd.DataFrame | None:
    """巨潮公司概况；“所属行业”作为当前行业归属的第二回退。

    直接调用与 AKShare 相同的公开接口，以补上其缺失的 HTTP 超时。
    """
    try:
        import requests
        from akshare.stock.stock_profile_cninfo import _get_file_content_ths
        from py_mini_racer import py_mini_racer

        js = py_mini_racer.MiniRacer()
        js.eval(_get_file_content_ths("cninfo.js"))
        response = requests.post(
            "https://webapi.cninfo.com.cn/api/sysapi/p_sysapi1133",
            params={"scode": symbol},
            headers=_cninfo_headers(js.call("getResCode1")),
            timeout=(5, 15),
        )
        response.raise_for_status()
        payload = response.json()
        records = payload.get("records") or []
        if payload.get("count") != 1 or not records:
            return None
        values = list(records[0].values())
        columns = [
            "公司名称", "英文名称", "曾用简称", "A股代码", "A股简称",
            "B股代码", "B股简称", "H股代码", "H股简称", "入选指数",
            "所属市场", "所属行业", "法人代表", "注册资金", "成立日期",
            "上市日期", "官方网站", "电子邮箱", "联系电话", "传真",
            "注册地址", "办公地址", "邮政编码", "主营业务", "经营范围",
            "机构简介",
        ]
        # 巨潮尾部附带四个接口内部字段，AKShare 同样会将它们剔除。
        if len(values) < len(columns):
            return None
        return pd.DataFrame([values[: len(columns)]], columns=columns)
    except Exception as exc:
        logger.warning("cninfo 公司概况调用失败（%s）：%s", symbol, exc)
        return None


def _cninfo_headers(enckey: str) -> dict[str, str]:
    return {
        "Accept": "*/*",
        "Accept-Encoding": "gzip, deflate",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Cache-Control": "no-cache",
        "Content-Length": "0",
        "Host": "webapi.cninfo.com.cn",
        "Accept-Enckey": enckey,
        "Origin": "https://webapi.cninfo.com.cn",
        "Pragma": "no-cache",
        "Referer": "https://webapi.cninfo.com.cn/",
        "X-Requested-With": "XMLHttpRequest",
    }


def fetch_name_change_hist(symbol: str) -> pd.DataFrame | None:
    """历史名称/曾用名（ak.stock_info_change_name，新浪公司资料页）。

    symbol 为 6 位代码。返回行序号 index（旧->新，1 起）与 name（含曾用名
    区间标记如 "中科健A->ST科健->..."）；接口不带日期，区间由解析方推导。
    注意：ak.stock_name_change_hist 在当前 akshare 版本已移除。
    """
    return _call("stock_info_change_name", symbol=symbol)
