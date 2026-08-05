"""固定因子结构、必需项阻断、可选项保守填充测试。"""

from app.services.stock_factors import (
    FactorResult,
    MISSING_OPTIONAL_PENALTY,
    _combine_family,
)


def test_optional_missing_uses_fixed_penalty_without_weight_renormalization() -> None:
    result = FactorResult(
        code="000001",
        name="完整性测试",
        industry="制造",
        zscores={"bp": 1.0, "ep": 1.0},
        model_structure={"sector": "industrial_or_legacy"},
    )
    score, structure = _combine_family(result, "value")
    assert score is not None
    assert structure["status"] == "valid"
    assert "dividend_yield" in structure["missing_optional"]
    weights = structure["effective_weights"]
    assert len(set(weights.values())) == 1
    expected = (2.0 + 5 * MISSING_OPTIONAL_PENALTY) / 7
    assert score == expected


def test_required_missing_blocks_family_instead_of_rewarding_sparse_stock() -> None:
    result = FactorResult(
        code="000002",
        name="稀疏测试",
        industry="制造",
        zscores={"ep": 5.0},
        model_structure={"sector": "industrial_or_legacy"},
    )
    score, structure = _combine_family(result, "value")
    assert score is None
    assert structure["status"] == "blocked_required_missing"
    assert structure["missing_required"] == ["bp"]
