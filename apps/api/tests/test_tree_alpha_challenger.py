"""树模型只能作为 challenger，且必须满足前置与多窗口晋级门禁。"""

from app.services.tree_alpha_challenger import (
    TreePrerequisites,
    prerequisite_gate,
    promotion_gate,
    run_xgboost_rank_challenger,
)


def test_tree_model_is_blocked_before_pit_and_registration() -> None:
    prerequisites = TreePrerequisites(False, True, False, True)
    passed, reasons = prerequisite_gate(prerequisites)
    assert passed is False
    assert len(reasons) == 2
    result = run_xgboost_rank_challenger([], prerequisites=prerequisites)
    assert result["status"] == "blocked_prerequisites"


def test_one_lucky_window_can_never_promote_tree_model() -> None:
    result = promotion_gate(
        [{"tree_net_rank_ic": 0.20, "ridge_net_rank_ic": 0.01}]
    )
    assert result["passed"] is False
    assert result["status"] == "challenger_only"
    stable = promotion_gate(
        [
            {"tree_net_rank_ic": 0.03, "ridge_net_rank_ic": 0.01},
            {"tree_net_rank_ic": 0.04, "ridge_net_rank_ic": 0.02},
            {"tree_net_rank_ic": 0.05, "ridge_net_rank_ic": 0.03},
        ]
    )
    assert stable["passed"] is True
    assert "review" in stable["status"]
