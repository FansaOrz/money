"""深度学习/RL 不得成为当前核心 Alpha 的机器可执行治理测试。"""

from app.services.model_governance import (
    production_model_gate,
    reinforcement_learning_scope,
)


def test_deep_and_rl_alpha_are_challenger_only() -> None:
    assert production_model_gate("rules_multifactor")["allowed"] is True
    assert production_model_gate("deep_neural_alpha")["allowed"] is False
    assert production_model_gate("reinforcement_learning_alpha")["allowed"] is False


def test_rl_research_is_limited_to_execution_or_contextual_bandit() -> None:
    execution = reinforcement_learning_scope("optimal_execution")
    assert execution["research_allowed"] is True
    assert execution["production_allowed"] is False
    assert execution["oms_rms_bypass_allowed"] is False
    assert reinforcement_learning_scope("core_alpha")["research_allowed"] is False
