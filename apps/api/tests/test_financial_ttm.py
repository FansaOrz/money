"""PIT TTM 构造的报告期黄金样例与反未来数据测试。"""

from datetime import date

import pytest

from app.services.financial_ttm import build_pit_ttm
from app.services.stock_repository import Fundamentals


def _snapshot(
    period: date,
    revenue: float,
    *,
    available_at: date | None = None,
    profit: float | None = None,
    ocf: float | None = None,
    capex: float | None = None,
) -> Fundamentals:
    return Fundamentals(
        code="000001",
        available_at=available_at or period,
        period=period,
        revenue=revenue,
        net_income=profit if profit is not None else revenue / 10,
        operating_cash_flow=ocf if ocf is not None else revenue / 5,
        capital_expenditure=capex if capex is not None else revenue / 20,
        flow_basis="year_to_date",
    )


@pytest.mark.parametrize(
    ("period", "expected"),
    [
        (date(2025, 3, 31), 130.0),
        (date(2025, 6, 30), 140.0),
        (date(2025, 9, 30), 150.0),
        (date(2025, 12, 31), 160.0),
    ],
)
def test_q1_h1_q3_fy_ttm_golden_examples(
    period: date,
    expected: float,
) -> None:
    rows = [
        _snapshot(date(2024, 3, 31), 20),
        _snapshot(date(2024, 6, 30), 50),
        _snapshot(date(2024, 9, 30), 80),
        _snapshot(date(2024, 12, 31), 120),
        _snapshot(date(2025, 3, 31), 30),
        _snapshot(date(2025, 6, 30), 70),
        _snapshot(date(2025, 9, 30), 110),
        _snapshot(date(2025, 12, 31), 160),
    ]
    by_period = {
        item.period: item for item in build_pit_ttm(rows)
    }
    result = by_period[period]
    assert result.revenue == expected
    assert result.net_income == expected / 10
    assert result.operating_cash_flow == expected / 5
    assert result.capital_expenditure == expected / 20
    assert result.free_cash_flow == pytest.approx(expected * 0.15)
    assert result.flow_basis == "TTM"


def test_missing_quarter_and_non_calendar_period_are_not_annualized() -> None:
    rows = [
        _snapshot(date(2024, 12, 31), 120),
        _snapshot(date(2025, 6, 30), 70),
        _snapshot(date(2025, 11, 30), 140),
    ]
    by_period = {
        item.period: item for item in build_pit_ttm(rows)
    }
    assert by_period[date(2025, 6, 30)].revenue is None
    assert by_period[date(2025, 11, 30)].revenue is None
    assert all(
        "TTM必要累计期缺失" in " ".join(item.financial_quality_reasons)
        for item in by_period.values()
        if item.period != date(2024, 12, 31)
    )


def test_future_restatement_cannot_change_historical_ttm() -> None:
    rows = [
        _snapshot(date(2024, 3, 31), 20, available_at=date(2024, 4, 20)),
        _snapshot(date(2024, 12, 31), 120, available_at=date(2025, 3, 20)),
        _snapshot(date(2025, 3, 31), 30, available_at=date(2025, 4, 20)),
        # 同报告期的事后更正，信号日不可见。
        _snapshot(date(2024, 12, 31), 999, available_at=date(2025, 6, 1)),
    ]
    results = build_pit_ttm(rows, as_of=date(2025, 4, 30))
    current = next(item for item in results if item.period == date(2025, 3, 31))
    assert current.revenue == 130.0
    assert date(2024, 12, 31) in current.ttm_component_periods
