"""PIT 流量财务累计值转单季/TTM；只使用信号日已公开版本。"""

from __future__ import annotations

from dataclasses import replace
from datetime import date

from app.services.stock_repository import Fundamentals

FLOW_FIELDS = (
    "revenue",
    "net_income",
    "operating_cash_flow",
    "capital_expenditure",
)


def _prior_periods(period: date) -> tuple[date, date] | None:
    if (period.month, period.day) not in ((3, 31), (6, 30), (9, 30)):
        return None
    return (
        date(period.year - 1, 12, 31),
        date(period.year - 1, period.month, period.day),
    )


def _ttm_value(
    current: Fundamentals,
    by_period: dict[date, Fundamentals],
    field: str,
) -> tuple[float | None, tuple[date, ...]]:
    if current.period is None:
        return None, ()
    value = getattr(current, field)
    if (current.period.month, current.period.day) == (12, 31):
        return value, (current.period,) if value is not None else ()
    previous = _prior_periods(current.period)
    if previous is None:
        return None, ()
    previous_fy = by_period.get(previous[0])
    previous_same = by_period.get(previous[1])
    if previous_fy is None or previous_same is None:
        return None, ()
    components = (
        value,
        getattr(previous_fy, field),
        getattr(previous_same, field),
    )
    if any(item is None for item in components):
        return None, ()
    return (
        float(components[0]) + float(components[1]) - float(components[2]),
        (current.period, previous[0], previous[1]),
    )


def build_pit_ttm(
    snapshots: list[Fundamentals],
    *,
    as_of: date | None = None,
) -> list[Fundamentals]:
    """按股票构造 TTM；缺少任一必要累计期则显式置空，不做年化猜测。"""
    grouped: dict[str, list[Fundamentals]] = {}
    for snapshot in snapshots:
        if as_of is not None and snapshot.available_at > as_of:
            continue
        grouped.setdefault(snapshot.code, []).append(snapshot)
    result: list[Fundamentals] = []
    for code in sorted(grouped):
        ordered = sorted(
            grouped[code],
            key=lambda item: (
                item.period or date.min,
                item.available_at,
            ),
        )
        by_period = {
            item.period: item for item in ordered if item.period is not None
        }
        for current in ordered:
            values: dict[str, float | None] = {}
            component_periods: set[date] = set()
            for field in FLOW_FIELDS:
                value, components = _ttm_value(current, by_period, field)
                values[field] = value
                component_periods.update(components)
            reasons = list(current.financial_quality_reasons)
            missing = [
                field for field, value in values.items() if value is None
            ]
            if missing:
                reason = "TTM必要累计期缺失：" + ",".join(missing)
                if reason not in reasons:
                    reasons.append(reason)
            capex = values["capital_expenditure"]
            ocf = values["operating_cash_flow"]
            fcf = (
                ocf - capex
                if ocf is not None and capex is not None
                else None
            )
            result.append(
                replace(
                    current,
                    revenue=values["revenue"],
                    net_income=values["net_income"],
                    operating_cash_flow=ocf,
                    capital_expenditure=capex,
                    free_cash_flow=fcf,
                    free_cash_flow_definition=(
                        "TTM经营现金流-TTM购建固定资产等现金支出"
                    ),
                    flow_basis="TTM",
                    ttm_component_periods=tuple(sorted(component_periods)),
                    financial_quality_reasons=tuple(reasons),
                )
            )
    return result
