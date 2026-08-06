"""凸优化、真实成本、预测不确定性与不可行诊断。"""

import numpy as np

from app.services.convex_portfolio_optimizer import optimize_portfolio


def test_low_confidence_high_score_gets_less_capital() -> None:
    result = optimize_portfolio(
        codes=["certain", "uncertain"],
        alpha=[0.02, 0.03],
        alpha_standard_errors=[0.001, 0.03],
        covariance=np.eye(2) * 0.0001,
        current_weights=[0.0, 0.0],
        benchmark_weights=[0.5, 0.5],
        max_stock_weight=1.0,
        max_turnover=2.0,
        maximum_cash=0.0,
        max_tracking_error=1.0,
        max_annual_volatility=1.0,
    )
    assert result["passed"] is True
    assert result["weights"]["certain"] > result["weights"]["uncertain"]
    assert result["input_sha256"]
    assert result["constraint_slacks"]


def test_infeasible_constraints_return_machine_readable_conflict() -> None:
    result = optimize_portfolio(
        codes=["a", "b"],
        alpha=[0.01, 0.01],
        covariance=np.eye(2) * 0.0001,
        current_weights=[0.0, 0.0],
        benchmark_weights=[0.5, 0.5],
        max_stock_weight=0.10,
        maximum_cash=0.0,
        max_turnover=2.0,
        max_tracking_error=1.0,
        max_annual_volatility=1.0,
    )
    assert result["passed"] is False
    assert result["infeasibility"]["conflicts"][0]["constraint"].startswith(
        "max_stock_weight"
    )


def test_style_constraints_accept_per_factor_limits() -> None:
    result = optimize_portfolio(
        codes=["a", "b"],
        alpha=[0.02, 0.01],
        covariance=np.eye(2) * 0.0001,
        current_weights=[0.0, 0.0],
        benchmark_weights=[0.5, 0.5],
        style_exposures=np.asarray([[1.0, -1.0], [-1.0, 1.0]]),
        benchmark_style_exposures=[0.0, 0.0],
        max_style_active_exposure=[0.10, 0.20],
        max_stock_weight=1.0,
        maximum_cash=0.0,
        max_turnover=2.0,
        max_tracking_error=1.0,
        max_annual_volatility=1.0,
    )

    assert result["passed"] is True
    assert abs(result["weights"]["a"] - result["weights"]["b"]) <= 0.10 + 1e-6
