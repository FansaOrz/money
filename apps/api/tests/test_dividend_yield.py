"""PIT trailing dividend yield 口径、覆盖与反未来公告测试。"""

from datetime import date

import pytest

from app.services.dividend_yield import (
    DividendEvent,
    assess_dividend_coverage,
    calculate_trailing_dividend_yield,
)


def _event(
    ex_date: date,
    available_at: date,
    cash: float,
    key: str,
    revision: int = 1,
) -> DividendEvent:
    return DividendEvent(
        code="000001",
        ex_date=ex_date,
        available_at=available_at,
        cash_per_share=cash,
        event_key=key,
        revision=revision,
    )


def test_trailing_gross_cash_includes_special_dividend_and_deduplicates_revision() -> None:
    events = [
        _event(date(2025, 6, 1), date(2025, 5, 1), 0.3, "ordinary"),
        _event(date(2025, 8, 1), date(2025, 7, 1), 0.2, "special"),
        _event(date(2025, 6, 1), date(2025, 5, 2), 0.35, "ordinary", 2),
    ]
    result = calculate_trailing_dividend_yield(
        events,
        code="000001",
        as_of=date(2025, 12, 31),
        price=10.0,
        source_covered=True,
    )
    assert result.value == pytest.approx(0.055)
    assert result.trailing_cash_per_share == pytest.approx(0.55)
    assert result.event_count == 2


def test_future_announcement_and_future_ex_date_do_not_affect_history() -> None:
    events = [
        _event(date(2025, 6, 1), date(2025, 5, 1), 0.3, "known"),
        _event(date(2025, 10, 1), date(2026, 1, 2), 5.0, "future-announcement"),
        _event(date(2026, 2, 1), date(2025, 12, 1), 5.0, "future-ex-date"),
    ]
    result = calculate_trailing_dividend_yield(
        events,
        code="000001",
        as_of=date(2025, 12, 31),
        price=10.0,
        source_covered=True,
    )
    assert result.value == pytest.approx(0.03)


def test_zero_requires_confirmed_source_coverage_and_price() -> None:
    uncovered = calculate_trailing_dividend_yield(
        [],
        code="000001",
        as_of=date(2025, 12, 31),
        price=10.0,
        source_covered=False,
    )
    assert uncovered.value is None
    assert uncovered.status == "source_uncovered"
    covered = calculate_trailing_dividend_yield(
        [],
        code="000001",
        as_of=date(2025, 12, 31),
        price=10.0,
        source_covered=True,
    )
    assert covered.value == 0.0
    assert covered.status == "valid"
    report = assess_dividend_coverage([uncovered, covered], minimum=0.5)
    assert report.coverage == 0.5
    assert report.passed is True
    assert report.missing_by_status == {"source_uncovered": 1}
