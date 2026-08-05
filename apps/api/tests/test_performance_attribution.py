"""归因量纲、几何链接、标准 Brinson 与残差质量。"""

import pytest

from app.services.performance_attribution import (
    account_return_bridge,
    brinson_fachler,
    geometric_link,
)
from app.services.transaction_cost_analysis import order_tca


def test_geometric_contributions_bridge_exactly() -> None:
    result = geometric_link(
        [
            {"selection": 0.02, "fees": -0.001},
            {"selection": -0.01, "fees": -0.001},
        ]
    )
    assert sum(result["linked_contributions"].values()) == pytest.approx(
        result["total_return"]
    )


def test_book_error_is_residual_not_selection() -> None:
    result = account_return_bridge(
        total_return=0.02,
        direct_selection=0.005,
        industry_allocation=0.002,
        style=0.0,
        market=0.01,
        cash_drag=0.0,
        fees=-0.001,
        slippage=-0.001,
    )
    assert result["selection"] == 0.005
    assert result["residual_unexplained"] == pytest.approx(0.005)
    assert result["quality_warning"] is True


def test_brinson_and_order_tca_bridge() -> None:
    brinson = brinson_fachler(
        portfolio_weights={"银行": 0.6, "科技": 0.4},
        benchmark_weights={"银行": 0.5, "科技": 0.5},
        portfolio_returns={"银行": 0.02, "科技": 0.03},
        benchmark_returns={"银行": 0.01, "科技": 0.02},
    )
    assert brinson["method"] == "Brinson-Fachler"
    tca = order_tca(
        side="buy",
        shares=100,
        decision_price=10.0,
        arrival_price=10.1,
        market_vwap=10.15,
        close_price=10.3,
        fill_price=10.2,
        fee=5.0,
    )
    assert tca["bridge_error"] == pytest.approx(0.0)
