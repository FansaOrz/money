"""小分母现金质量与 PIT TTM FCF yield 的经济口径测试。"""

from datetime import date

import pytest

from app.services.financial_ratio_policy import (
    assess_cash_conversion,
    assess_fcf_yield,
)


@pytest.mark.parametrize("profit", [1.0, -1.0, 1000.0, -1000.0])
def test_tiny_positive_or_negative_profit_never_creates_extreme_ratio(
    profit: float,
) -> None:
    result = assess_cash_conversion(
        net_income=profit,
        operating_cash_flow=50_000_000.0,
        total_assets=1_000_000_000.0,
    )
    assert result.ocf_to_profit is None
    assert result.cash_conversion_assets is not None
    assert abs(result.cash_conversion_assets) < 0.1


def test_normal_profit_keeps_display_ratio_but_stable_metric_is_asset_scaled() -> None:
    result = assess_cash_conversion(
        net_income=10_000_000.0,
        operating_cash_flow=15_000_000.0,
        total_assets=1_000_000_000.0,
    )
    assert result.ocf_to_profit == pytest.approx(1.5)
    assert result.cash_conversion_assets == pytest.approx(0.005)


def test_fcf_yield_requires_ttm_and_is_disabled_for_finance() -> None:
    common = {
        "free_cash_flow": 100.0,
        "flow_basis": "TTM",
        "market_cap": 1000.0,
        "market_cap_date": date(2025, 12, 30),
        "signal_date": date(2025, 12, 31),
        "unit_policy": "CNY元",
    }
    valid = assess_fcf_yield(**common, is_financial_company=False)
    assert valid.value == pytest.approx(0.1)
    assert valid.lineage["currency"] == "CNY"
    finance = assess_fcf_yield(**common, is_financial_company=True)
    assert finance.value is None
    assert finance.status == "not_applicable_financial"
    non_ttm = assess_fcf_yield(
        **{**common, "flow_basis": "year_to_date"},
        is_financial_company=False,
    )
    assert non_ttm.value is None
    assert non_ttm.status == "non_ttm"


def test_extreme_fcf_yield_is_sent_to_review() -> None:
    result = assess_fcf_yield(
        free_cash_flow=10_000.0,
        flow_basis="TTM",
        market_cap=1_000.0,
        market_cap_date=date(2025, 12, 31),
        signal_date=date(2025, 12, 31),
        is_financial_company=False,
        unit_policy="CNY元",
    )
    assert result.value is None
    assert result.status == "economic_extreme"
