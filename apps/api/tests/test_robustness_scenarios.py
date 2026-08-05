"""稳健性目录完整性与保守情景门禁。"""

from app.services.robustness_scenarios import (
    REQUIRED_DIMENSIONS,
    evaluate_robustness,
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
