"""稳健性目录完整性与保守情景门禁。"""

from datetime import date, timedelta

from app.services.stock_backtest import (
    BacktestConfig,
    BacktestOutcome,
    RebalanceDetail,
)
from app.services.robustness_scenarios import (
    REQUIRED_DIMENSIONS,
    evaluate_robustness,
    run_validation_robustness,
)


def _rows(value: float) -> list[dict[str, object]]:
    return [
        {"dimension": dimension, "case": "test", "net_excess_return": value}
        for dimension in sorted(REQUIRED_DIMENSIONS)
    ]


def test_all_neighbors_and_conservative_costs_must_be_stable() -> None:
    stable = evaluate_robustness(_rows(0.01))
    assert stable["passed"] is True
    fragile_rows = _rows(0.01)
    next(
        row for row in fragile_rows if row["dimension"] == "cost_3x"
    )["net_excess_return"] = -0.01
    fragile = evaluate_robustness(fragile_rows)
    assert fragile["passed"] is False


def test_missing_dimension_fails_closed() -> None:
    result = evaluate_robustness(
        [row for row in _rows(0.01) if row["dimension"] != "data_late"]
    )
    assert result["passed"] is False
    assert result["missing_dimensions"] == ["data_late"]


def test_validation_runner_executes_every_preregistered_dimension() -> None:
    start = date(2025, 1, 1)
    calendar = [start + timedelta(days=index) for index in range(12)]
    baseline = BacktestOutcome(
        calendar=calendar,
        equity=[1_000_000 * (1.001**index) for index in range(12)],
        daily_returns=[0.001] * 11,
        benchmark=[1.0] * 12,
        benchmark_kind="test",
        rebalances=[
            RebalanceDetail(
                signal_date=calendar[0],
                target={"600001": 1.0},
                fills=[object()],
                turnover=0.5,
            )
        ],
        final_value=1_000_000 * (1.001**11),
        total_fees=10.0,
        avg_turnover=0.5,
        forward_returns=[],
        scores_by_date=[],
        groups_by_date=[
            (calendar[0], {"600001": ("银行", "large")})
        ],
    )
    calls: list[BacktestConfig] = []

    def fake_run_backtest(*, config, repository):
        del repository
        calls.append(config)
        return baseline

    rows = run_validation_robustness(
        object(),
        BacktestConfig(start=calendar[0], end=calendar[-1]),
        baseline,
        run_backtest_fn=fake_run_backtest,
    )

    assert {row["dimension"] for row in rows} == REQUIRED_DIMENSIONS
    assert len(calls) >= 20
    assert evaluate_robustness(rows)["passed"] is True
    assert all(
        row["source"] == "validation_baseline_leave_block_out"
        for row in rows
        if row["dimension"] == "delete_period"
    )
