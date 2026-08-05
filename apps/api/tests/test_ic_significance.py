"""IC 稳健显著性、块置信区间和多重检验门禁测试。"""

from datetime import date

from app.services.ic_significance import factor_ic_significance


def test_weak_ic_crossing_zero_is_not_proven_after_fdr() -> None:
    factor_dates = []
    forward_dates = []
    for period in range(15):
        values = {f"{index:06d}": float(index) for index in range(30)}
        returns = {
            code: (
                value if period % 2 == 0 else -value
            )
            for code, value in values.items()
        }
        day = date(2024 + period // 12, period % 12 + 1, 1)
        factor_dates.append((day, {"composite": values, "value": values}))
        forward_dates.append((day, returns))
    report = factor_ic_significance(
        factor_dates,
        forward_dates,
        extra_attempt_pvalues={f"trial-{index}": 0.05 for index in range(20)},
    )
    composite = report["factors"]["composite"]
    assert composite["proven_positive"] is False
    assert report["status"] == "alpha_evidence_insufficient"
    assert report["tested_hypotheses"] == 22
    assert composite["effective_observations"] <= 15
