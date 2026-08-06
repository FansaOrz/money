"""版本11训练预检的阶段门禁测试。"""

from scripts.run_strategy_v11_training_preflight import (
    _robustness_precondition,
)


def test_robustness_requires_all_baseline_investment_gates() -> None:
    result = _robustness_precondition(
        ic={"factors": {"composite": {"proven_positive": True}}},
        quintile={"passed": True},
        active={"passed": True},
        development_metrics={"net_excess_return": 0.01},
    )

    assert result["passed"] is True
    assert result["status"] == "eligible"
    assert result["failures"] == []


def test_positive_excess_cannot_bypass_failed_alpha_evidence() -> None:
    result = _robustness_precondition(
        ic={"factors": {"composite": {"proven_positive": False}}},
        quintile={"passed": False},
        active={"passed": False},
        development_metrics={"net_excess_return": 0.08},
    )

    assert result["passed"] is False
    assert result["status"] == "blocked_by_baseline"
    assert result["failures"] == [
        "composite_ic_proven_positive",
        "quintile_passed",
        "active_alpha_passed",
    ]
