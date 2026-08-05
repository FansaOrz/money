"""尾部风险、离散化、压力和容量闭环。"""

import numpy as np

from app.services.portfolio_risk_controls import (
    capacity_curve,
    discretize_portfolio,
    stress_test,
    tail_risk,
)


def test_left_tail_is_penalized_despite_same_typical_volatility() -> None:
    benign = [0.01, -0.01] * 49 + [-0.02, 0.02]
    crash = [0.01, -0.01] * 49 + [-0.20, 0.20]
    assert tail_risk(crash)["cvar"] < tail_risk(benign)["cvar"]


def test_discrete_portfolio_rechecks_real_constraints() -> None:
    result = discretize_portfolio(
        target_weights={"600001": 0.5, "600002": 0.5},
        prices={"600001": 10.0, "600002": 20.0},
        portfolio_value=10_000,
        lot_sizes={"600001": 100, "600002": 100},
        covariance=np.eye(2) * 0.0001,
        benchmark_weights={"600001": 0.5, "600002": 0.5},
        max_stock_weight=0.6,
    )
    assert result["shares"]["600001"] % 100 == 0
    assert result["cash"] >= 0
    assert result["passed"] is True


def test_stress_and_capacity_are_explainable_per_stock() -> None:
    stress = stress_test(
        codes=["a", "b"],
        weights=[0.5, 0.5],
        position_values=[500_000, 500_000],
        adv_amounts=[10_000_000, 1_000_000],
        industry_by_code={"a": "银行", "b": "科技"},
        consecutive_limit_down_codes={"b"},
    )
    assert stress["passed"] is False
    assert stress["liquidation"]["b"]["tradable"] is False
    capacity = capacity_curve(
        codes=["a", "b"],
        target_weights=[0.5, 0.5],
        adv_amounts=[10_000_000, 1_000_000],
        capital_levels=[100_000, 10_000_000],
        gross_expected_return=0.10,
    )
    assert capacity["curve"][1]["market_impact"] > capacity["curve"][0]["market_impact"]
