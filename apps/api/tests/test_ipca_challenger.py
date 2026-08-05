"""IPCA 仅研究 challenger，并能恢复简单隐含特征结构。"""

from datetime import date

from app.services.ipca_challenger import (
    fit_ipca,
    ipca_research_gate,
    predict_expected_returns,
    summarize_ipca_fit,
)
from app.services.linear_alpha_challenger import AlphaRow, FEATURES


def _synthetic() -> list[AlphaRow]:
    rows = []
    for month in range(1, 25):
        year = 2023 + (month - 1) // 12
        calendar_month = (month - 1) % 12 + 1
        day = date(year, calendar_month, 1)
        factor_return = 0.01 + month / 10_000
        for index in range(30):
            quality = (index - 15) / 10
            features = {name: 0.0 for name in FEATURES}
            features["quality"] = quality
            rows.append(
                AlphaRow(
                    signal_date=day,
                    code=f"{index:06d}",
                    industry="制造",
                    features=features,
                    forward_return=quality * factor_return,
                )
            )
    return rows


def test_ipca_fit_is_explainable_but_stays_challenger() -> None:
    rows = _synthetic()
    fit = fit_ipca(rows, factors=1)
    summary = summarize_ipca_fit(fit)
    assert summary["status"] == "challenger_only"
    assert fit.train_r_squared > 0.99
    predictions = predict_expected_returns(fit, rows[:30])
    assert predictions["000029"] > predictions["000000"]


def test_short_history_or_weak_oos_explanation_cannot_promote() -> None:
    result = ipca_research_gate(
        monthly_periods=24,
        oos_r_squared=0.20,
        explicit_factor_oos_r_squared=0.10,
        loading_sign_stability=0.95,
    )
    assert result["passed"] is False
    assert result["production_enabled"] is False
