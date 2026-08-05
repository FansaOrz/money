"""可复现、黄金账本、性质不变量、泄漏、混沌与研究 challenger。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import numpy as np
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from app.services.chaos_harness import FAILURE_TYPES, run_drill
from app.services.benchmark_data import transparent_style_benchmarks
from app.services.data_leakage_guard import (
    FIELD_AVAILABILITY,
    assert_point_in_time_inputs,
    history_unchanged_after_future_mutation,
)
from app.services.execution_baselines import almgren_chriss, twap, vwap
from app.services.high_risk_preflight import evaluate_preflight
from app.services.ledger_replay import replay_backtest, replay_forward
from app.services.multi_period_optimizer import optimize_multi_period
from app.services.regime_challenger import gaussian_state_probabilities
from app.services.reproducibility import configure_determinism
from app.services.risk_overlay import simple_risk_overlay


GOLDEN_EVENTS = [
    {"id": "buy", "type": "buy", "code": "A", "quantity": 100, "price": 10, "fee": 1},
    {"id": "suspend", "type": "suspension", "code": "A"},
    {"id": "limit", "type": "limit_block", "code": "A"},
    {"id": "partial", "type": "sell", "code": "A", "quantity": 20, "price": 11, "fee": 1},
    {"id": "tax", "type": "tax", "amount": 5},
    {"id": "record", "type": "dividend_record", "code": "A", "cash_per_share": "0.1"},
    {"id": "pay", "type": "dividend_pay", "record_event_id": "record"},
    {"id": "bonus", "type": "bonus", "code": "A", "ratio": "1.1"},
    {"id": "rights", "type": "rights", "code": "A", "ratio": "0.1", "price": "5"},
    {"id": "delist", "type": "delist_notice", "code": "A"},
    {"id": "swap", "type": "swap", "code": "A", "target_code": "B", "ratio": "0.5"},
]


def test_golden_ledger_replay_is_identical_and_idempotent() -> None:
    events = GOLDEN_EVENTS + [GOLDEN_EVENTS[0]]
    backtest = replay_backtest(events, Decimal("10000"))
    forward = replay_forward(events, Decimal("10000"))
    assert backtest == forward
    assert backtest[-1] == {
        "event_id": "swap",
        "cash": "9177.00",
        "positions": {"B": "48.400"},
        "receivables": {},
        "fees": "7",
        "net_asset_value": "10145.00",
    }


@settings(max_examples=80, deadline=None, derandomize=True)
@given(
    quantity=st.integers(min_value=1, max_value=1_000_000),
    intervals=st.integers(min_value=1, max_value=30),
)
def test_execution_allocations_conserve_quantity(quantity: int, intervals: int) -> None:
    profile = [index + 1 for index in range(intervals)]
    for allocation in (
        twap(quantity, intervals),
        vwap(quantity, profile),
        almgren_chriss(
            quantity,
            intervals,
            risk_aversion=1e-6,
            volatility=0.02,
            temporary_impact=0.001,
        ),
    ):
        assert sum(allocation) == quantity
        assert all(item >= 0 for item in allocation)


def test_every_input_has_availability_and_future_mutation_is_inert() -> None:
    assert {"daily.volume", "daily.close", "financial.restated_value"} <= set(
        FIELD_AVAILABILITY
    )
    decision = datetime(2025, 1, 1, 7, tzinfo=UTC)
    rows = [
        {"field": "daily.open", "value": 10, "available_at": decision},
        {
            "field": "daily.close",
            "value": 11,
            "available_at": decision + timedelta(hours=8),
        },
    ]

    def builder(data, at):
        return [row["value"] for row in data if row["available_at"] <= at]

    assert history_unchanged_after_future_mutation(
        builder, rows, decision_at=decision
    )
    with pytest.raises(ValueError, match="T 后字段"):
        assert_point_in_time_inputs(rows, decision_at=decision)


def test_chaos_fail_closed_for_every_registered_failure() -> None:
    def operation(failure, _key):
        raise OSError(failure)

    for failure in FAILURE_TYPES:
        report = run_drill(failure, operation, idempotency_key=f"drill-{failure}")
        assert report["safe_stopped"] is True
        assert report["alert_required"] is True


def test_preflight_requires_clean_evidence_and_exact_second_confirmation() -> None:
    first = evaluate_preflight(
        operation="restore",
        target="account-A",
        impact="replace isolated recovery database",
        data_fresh=True,
        clean_workspace=True,
        evidence_consistent=True,
        ledger_balanced=True,
        idempotency_key="restore-1",
    )
    assert first["allowed"] is False
    approved = evaluate_preflight(
        operation="restore",
        target="account-A",
        impact="replace isolated recovery database",
        data_fresh=True,
        clean_workspace=True,
        evidence_consistent=True,
        ledger_balanced=True,
        idempotency_key="restore-1",
        confirmation_digest=first["confirmation_digest"],
    )
    assert approved["allowed"] is True
    blocked = evaluate_preflight(
        operation="order",
        target="A",
        impact="buy",
        data_fresh=False,
        clean_workspace=True,
        evidence_consistent=True,
        ledger_balanced=True,
        idempotency_key="order-1",
    )
    assert "数据过期" in blocked["blockers"]


def test_research_overlays_are_explicit_challengers_and_deterministic() -> None:
    first = configure_determinism(7)
    a = np.random.random(5)
    second = configure_determinism(7)
    b = np.random.random(5)
    assert np.array_equal(a, b)
    assert first["sha256"] == second["sha256"]
    overlay = simple_risk_overlay([0.001] * 120, current_drawdown=-0.05)
    assert overlay["status"] == "challenger"
    probabilities = gaussian_state_probabilities([0.001, 0.08])
    assert probabilities[-1]["stress_probability"] > probabilities[0]["stress_probability"]
    optimized = optimize_multi_period(
        np.array([[0.02, 0.01], [0.00, 0.03]]),
        np.array([0.5, 0.5]),
        return_error_bound=0.01,
    )
    assert optimized["status"] == "challenger"
    assert optimized["solver_status"] in {"optimal", "optimal_inaccurate"}


def test_transparent_style_benchmarks_share_period_calendar() -> None:
    periods = [
        {
            f"S{index}": {
                "forward_return": index / 100,
                "low_volatility": float(10 - index),
                "value": float(index),
                "quality": float(index),
            }
            for index in range(10)
        }
    ]
    curves = transparent_style_benchmarks(periods)
    assert set(curves) == {"low_volatility", "value", "quality"}
    assert all(len(curve) == 2 for curve in curves.values())
