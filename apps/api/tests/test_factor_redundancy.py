"""相关/VIF/边际和条件 IC 的冗余因子诊断测试。"""

from datetime import date

from app.services.factor_redundancy import (
    REDUNDANCY_FACTORS,
    diagnose_factor_redundancy,
)


def test_redundant_factor_is_flagged_by_correlation_vif_and_conditional_ic() -> None:
    values = {name: {} for name in REDUNDANCY_FACTORS}
    returns = {}
    for index in range(80):
        code = f"{index:06d}"
        base = (index - 40) / 40
        values["momentum_12_1"][code] = base
        values["momentum_6_1"][code] = base * 2.0
        values["residual_momentum"][code] = ((index * 7) % 17) / 17
        values["trend"][code] = ((index * 11) % 19) / 19
        values["volatility_60"][code] = ((index * 3) % 23) / 23
        values["volatility_120"][code] = ((index * 5) % 29) / 29
        values["max_drawdown_120"][code] = ((index * 13) % 31) / 31
        values["residual_volatility"][code] = ((index * 17) % 37) / 37
        returns[code] = base * 0.02
    report = diagnose_factor_redundancy(
        [(date(2025, 1, 31), values)],
        [(date(2025, 1, 31), returns)],
    )
    pair = "momentum_12_1|momentum_6_1"
    assert report["correlation_mean"][pair] > 0.99
    assert report["vif_mean"]["momentum_12_1"] > 100
    assert any(
        action["pair"] == ["momentum_12_1", "momentum_6_1"]
        for action in report["actions"]
    )


def test_one_unavailable_factor_does_not_blank_all_redundancy_metrics() -> None:
    values = {name: {} for name in REDUNDANCY_FACTORS}
    returns = {}
    for index in range(40):
        code = f"{index:06d}"
        for offset, factor in enumerate(REDUNDANCY_FACTORS):
            values[factor][code] = (
                None
                if factor == "residual_momentum"
                else index + ((index * (offset + 3)) % 11) / 10
            )
        returns[code] = index / 1000

    report = diagnose_factor_redundancy(
        [(date(2025, 1, 31), values)],
        [(date(2025, 1, 31), returns)],
    )

    assert report["vif_mean"]["momentum_12_1"] is not None
    assert report["marginal_rank_ic_mean"]["momentum_12_1"] is not None
    assert report["conditional_rank_ic_mean"]["momentum_12_1"] is not None
    assert report["vif_mean"]["residual_momentum"] is None
    assert report["unavailable_factors"] == ["residual_momentum"]
