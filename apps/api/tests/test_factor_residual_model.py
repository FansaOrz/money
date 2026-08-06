"""外部/不含自身市场代理和残差模型诊断测试。"""

from datetime import date, timedelta

import pytest

from app.services.factor_residual_model import (
    build_return_proxy_aggregate,
    estimate_residual_model,
    leave_one_out_from_aggregate,
    leave_one_out_proxy,
)
from app.services.stock_factors import residual_momentum_from_returns


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


def test_preaggregated_leave_one_out_numerically_matches_direct_scan() -> None:
    start = date(2025, 1, 1)
    returns = {
        code: {
            start + timedelta(days=index): (index + offset) / 10_000
            for index in range(80)
            if not (offset == 3 and index % 11 == 0)
        }
        for offset, code in enumerate(("A", "B", "C", "D", "E", "F", "G"))
    }
    aggregate = build_return_proxy_aggregate(returns)

    by_date: dict[date, list[float]] = {}
    for code, series in returns.items():
        if code == "C":
            continue
        for day, value in series.items():
            by_date.setdefault(day, []).append(value)
    expected = {
        day: sum(values) / len(values)
        for day, values in by_date.items()
        if len(values) >= 5
    }
    actual = leave_one_out_from_aggregate(aggregate, "C", returns["C"])

    assert actual.keys() == expected.keys()
    for day, value in expected.items():
        assert actual[day] == pytest.approx(value, abs=1e-15)


def test_market_and_industry_model_saves_window_and_diagnostics() -> None:
    start = date(2025, 1, 1)
    market = {start + timedelta(days=i): (i % 7 - 3) / 1000 for i in range(100)}
    industry = {
        day: value * 0.5 + ((index % 5) - 2) / 2000
        for index, (day, value) in enumerate(market.items())
    }
    stock = {day: 0.0001 + 1.2 * market[day] + 0.8 * industry[day] for day in market}
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


def test_residual_momentum_matures_from_252_returns_not_253_prices() -> None:
    residuals = [0.001] * 252

    value = residual_momentum_from_returns(residuals)

    assert value == pytest.approx((1.001**231) - 1.0)
    assert residual_momentum_from_returns(residuals[:-1]) is None
