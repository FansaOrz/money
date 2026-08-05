"""外部/不含自身市场代理和残差模型诊断测试。"""

from datetime import date, timedelta

import pytest

from app.services.factor_residual_model import (
    estimate_residual_model,
    leave_one_out_proxy,
)


def test_leave_one_out_proxy_is_unchanged_when_own_stock_changes() -> None:
    start = date(2025, 1, 1)
    original = {
        "A": {start + timedelta(days=i): i / 1000 for i in range(80)},
        "B": {start + timedelta(days=i): i / 2000 for i in range(80)},
        "C": {start + timedelta(days=i): -i / 3000 for i in range(80)},
    }
    before = leave_one_out_proxy(original, "A")
    original["A"] = {day: value * 100 for day, value in original["A"].items()}
    after = leave_one_out_proxy(original, "A")
    assert before == after


def test_market_and_industry_model_saves_window_and_diagnostics() -> None:
    start = date(2025, 1, 1)
    market = {start + timedelta(days=i): (i % 7 - 3) / 1000 for i in range(100)}
    industry = {
        day: value * 0.5 + ((index % 5) - 2) / 2000
        for index, (day, value) in enumerate(market.items())
    }
    stock = {
        day: 0.0001 + 1.2 * market[day] + 0.8 * industry[day]
        for day in market
    }
    result = estimate_residual_model(
        stock,
        market,
        market_source="official_total_return_index",
        industry_returns=industry,
    )
    assert result.model == "market_plus_industry"
    assert result.beta == pytest.approx(1.2, abs=1e-8)
    assert result.industry_beta == pytest.approx(0.8, abs=1e-8)
    assert result.r_squared == pytest.approx(1.0)
    assert result.window == 252
    assert result.observations == 100
