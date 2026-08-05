"""A 股按板块、上市阶段和生效日期版本化的涨跌幅规则。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True)
class PriceLimitRule:
    version: str
    upper_limit: float | None
    lower_limit: float | None
    no_limit_reason: str | None = None


def price_limit_rule(
    code: str,
    trade_date: date,
    *,
    st: bool,
    listing_session: int | None = None,
    delisting_period: bool = False,
) -> PriceLimitRule:
    """返回当日适用幅度；None 表示制度明确规定当日无涨跌幅限制。"""
    normalized = str(code).split(".")[0].zfill(6)
    if normalized.startswith(("688", "689")):
        if trade_date < date(2019, 7, 22):
            raise ValueError("科创板成立前不存在涨跌幅规则")
        if listing_session is not None and listing_session <= 5:
            return PriceLimitRule(
                "SSE_STAR_IPO_FIRST5_NO_LIMIT_20190722",
                None,
                None,
                "科创板上市前5个交易日无涨跌幅限制",
            )
        if delisting_period:
            return PriceLimitRule(
                "SSE_STAR_DELISTING_20PCT", 0.20, 0.20
            )
        return PriceLimitRule("SSE_STAR_20PCT_20190722", 0.20, 0.20)
    if normalized.startswith("30"):
        if trade_date >= date(2020, 8, 24):
            if listing_session is not None and listing_session <= 5:
                return PriceLimitRule(
                    "SZSE_CHINEXT_IPO_FIRST5_NO_LIMIT_20200824",
                    None,
                    None,
                    "注册制创业板上市前5个交易日无涨跌幅限制",
                )
            if delisting_period:
                return PriceLimitRule(
                    "SZSE_CHINEXT_DELISTING_20PCT", 0.20, 0.20
                )
            return PriceLimitRule("SZSE_CHINEXT_20PCT_20200824", 0.20, 0.20)
        if listing_session == 1:
            return PriceLimitRule(
                "SZSE_IPO_FIRST_DAY_44_36_LEGACY", 0.44, 0.36
            )
        return PriceLimitRule(
            "SZSE_CHINEXT_10PCT_20091030",
            0.05 if st else 0.10,
            0.05 if st else 0.10,
        )
    if normalized.startswith(("4", "8", "92")):
        if trade_date < date(2021, 11, 15):
            raise ValueError("北交所成立前不存在涨跌幅规则")
        if listing_session == 1:
            return PriceLimitRule(
                "BSE_IPO_FIRST_DAY_NO_LIMIT_20211115",
                None,
                None,
                "北交所上市首日无涨跌幅限制",
            )
        if delisting_period:
            return PriceLimitRule("BSE_DELISTING_30PCT", 0.30, 0.30)
        return PriceLimitRule("BSE_30PCT_20211115", 0.30, 0.30)
    if listing_session == 1 and not delisting_period:
        return PriceLimitRule("MAIN_IPO_FIRST_DAY_44_36", 0.44, 0.36)
    if normalized.startswith("6"):
        if delisting_period:
            return PriceLimitRule("SSE_MAIN_DELISTING_10PCT", 0.10, 0.10)
        return PriceLimitRule(
            "SSE_MAIN_ST_5PCT" if st else "SSE_MAIN_10PCT",
            0.05 if st else 0.10,
            0.05 if st else 0.10,
        )
    if normalized.startswith(("00", "001", "002", "003")):
        if delisting_period:
            return PriceLimitRule("SZSE_MAIN_DELISTING_10PCT", 0.10, 0.10)
        return PriceLimitRule(
            "SZSE_MAIN_ST_5PCT" if st else "SZSE_MAIN_10PCT",
            0.05 if st else 0.10,
            0.05 if st else 0.10,
        )
    raise ValueError(f"无法识别证券 {code} 的涨跌幅板块")
