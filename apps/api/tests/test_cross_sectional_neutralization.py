"""WLS 行业/规模/Beta/流动性中性化测试。"""

import math

from app.services.cross_sectional_neutralization import (
    NeutralizationObservation,
    neutralize_wls,
)


def test_wls_residual_is_weighted_orthogonal_to_controls() -> None:
    rows = []
    for index in range(100):
        size = 20.0 + index / 100
        beta = 0.5 + (index % 13) / 10
        liquidity = 15.0 + (index % 17) / 20
        industry = "A" if index % 2 == 0 else "B"
        industry_effect = 1.5 if industry == "B" else 0.0
        noise = math.sin(index) * 0.001
        value = industry_effect + 2.0 * size - beta + 0.5 * liquidity + noise
        rows.append(
            NeutralizationObservation(
                code=f"{index:06d}",
                industry=industry,
                value=value,
                log_market_cap=size,
                beta=beta,
                liquidity=liquidity,
                float_market_cap=1e9 * (1 + index / 100),
            )
        )
    result = neutralize_wls(rows)
    assert result.method == "wls_sqrt_float_market_cap"
    assert result.r_squared is not None and result.r_squared > 0.99
    for correlation in result.weighted_control_correlations.values():
        assert correlation is None or abs(correlation) < 1e-7
    assert "industry[B]" in result.coefficients


def test_small_industry_is_pooled_and_small_cross_section_falls_back() -> None:
    rows = [
        NeutralizationObservation(
            code=str(index),
            industry="tiny" if index < 2 else "main",
            value=float(index),
            log_market_cap=float(index),
            beta=1.0,
            liquidity=10.0,
            float_market_cap=1e9,
        )
        for index in range(30)
    ]
    result = neutralize_wls(rows)
    assert result.small_industries == ("tiny",)
    fallback = neutralize_wls(rows[:5])
    assert fallback.method == "fallback_small_cross_section"
