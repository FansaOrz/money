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


def test_discrete_portfolio_rejects_unaffordable_and_dust_holdings() -> None:
    result = discretize_portfolio(
        target_weights={
            "300001": 0.05,
            "600001": 0.001,
            "600002": 0.049,
        },
        prices={"300001": 955.0, "600001": 50.0, "600002": 10.0},
        portfolio_value=1_000_000.0,
        lot_sizes={"300001": 100, "600001": 100, "600002": 100},
        minimum_holdings=2,
    )

    assert result["shares"]["300001"] == 0
    assert result["shares"]["600001"] == 0
    assert result["shares"]["600002"] == 4_900
    assert result["executable_holdings"] == 1
    assert result["passed"] is False
    assert any(
        item["constraint"] == "minimum_holdings" for item in result["violations"]
    )


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
