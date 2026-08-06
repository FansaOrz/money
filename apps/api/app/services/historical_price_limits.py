"""由权威前收盘价和版本化交易规则生成可审计的历史涨跌停价。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from app.services.price_limit_rules import price_limit_rule

ALGORITHM_VERSION = "VALIDATED_DERIVED_LIMIT_V2"
PRICE_TICK = Decimal("0.01")


@dataclass(frozen=True)
class DerivedPriceLimit:
    """一条带完整推导口径的历史涨跌停记录。"""

    code: str
    trade_date: date
    pre_close: float
    up_limit: float | None
    down_limit: float | None
    rule_version: str
    no_limit_reason: str | None
    algorithm_version: str = ALGORITHM_VERSION


def round_price_tick(value: float) -> float:
    """按 A 股分位价格单位进行确定性四舍五入。"""
    return float(Decimal(str(value)).quantize(PRICE_TICK, rounding=ROUND_HALF_UP))


def names_prove_non_st(names: list[str]) -> bool:
    """名称证据非空且从未出现 ST/退市标识时，才允许按普通股规则派生。"""
    normalized = [str(name).strip().upper() for name in names if str(name).strip()]
    return bool(normalized) and all(
        "ST" not in name and "退" not in name for name in normalized
    )


def dated_name_as_of(
    periods: list[tuple[date, date | None, str]],
    day: date,
) -> str | None:
    """从带起止日的名称证据中取得当日名称；冲突或缺口均拒绝猜测。"""
    active = [
        (start, name)
        for start, end, name in periods
        if start <= day and (end is None or day <= end)
    ]
    if not active:
        return None
    latest_start = max(start for start, _name in active)
    names = {
        name.strip() for start, name in active if start == latest_start and name.strip()
    }
    return next(iter(names)) if len(names) == 1 else None


def derive_price_limit(
    code: str,
    trade_date: date,
    pre_close: float,
    *,
    st: bool,
    listing_session: int | None = None,
    delisting_period: bool = False,
) -> DerivedPriceLimit:
    """用公开前收盘价和当日生效规则生成一条限价记录。"""
    if pre_close <= 0:
        raise ValueError("前收盘价必须为正数")
    rule = price_limit_rule(
        code,
        trade_date,
        st=st,
        listing_session=listing_session,
        delisting_period=delisting_period,
    )
    return DerivedPriceLimit(
        code=str(code).split(".")[0].zfill(6),
        trade_date=trade_date,
        pre_close=pre_close,
        up_limit=(
            round_price_tick(pre_close * (1.0 + rule.upper_limit))
            if rule.upper_limit is not None
            else None
        ),
        down_limit=(
            round_price_tick(pre_close * (1.0 - rule.lower_limit))
            if rule.lower_limit is not None
            else None
        ),
        rule_version=rule.version,
        no_limit_reason=rule.no_limit_reason,
    )


__all__ = [
    "ALGORITHM_VERSION",
    "DerivedPriceLimit",
    "dated_name_as_of",
    "derive_price_limit",
    "names_prove_non_st",
    "round_price_tick",
]
