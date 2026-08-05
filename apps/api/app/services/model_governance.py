"""生产模型类型白名单与复杂 challenger 治理。"""

from __future__ import annotations

PRODUCTION_MODEL_TYPES = {
    "rules_multifactor",
    "shrunk_ic_linear",
    "approved_regularized_linear",
}
CHALLENGER_ONLY_MODEL_TYPES = {
    "xgboost_ranker",
    "lightgbm_ranker",
    "ipca",
    "deep_neural_alpha",
    "reinforcement_learning_alpha",
    "contextual_bandit",
}


def production_model_gate(model_type: str) -> dict[str, object]:
    if model_type in PRODUCTION_MODEL_TYPES:
        return {
            "allowed": True,
            "status": "production_type_allowed_subject_to_evidence",
        }
    if model_type in CHALLENGER_ONLY_MODEL_TYPES:
        return {
            "allowed": False,
            "status": "challenger_only",
            "reason": "复杂模型不得自动成为核心 Alpha 或绕过独立模型风险审查",
        }
    return {
        "allowed": False,
        "status": "unknown_model_type",
        "reason": "模型类型未登记",
    }


def reinforcement_learning_scope(scope: str) -> dict[str, object]:
    allowed_research = {"optimal_execution", "contextual_bandit"}
    return {
        "research_allowed": scope in allowed_research,
        "production_allowed": False,
        "required_baselines": ["TWAP", "VWAP", "rule_based_impact_model"],
        "oms_rms_bypass_allowed": False,
    }
