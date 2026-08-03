from app.services.fund_advice import build_fund_advice


def test_strong_trend_can_recommend_add() -> None:
    result = build_fund_advice(
        sample_count=500,
        trend_signal="strong_up",
        return_20d=0.04,
        return_60d=0.12,
        annual_volatility=0.18,
        max_drawdown=-0.12,
        sharpe=1.3,
    )
    assert result["action"] == "add"
    assert result["score"] >= 72
    assert result["reasons"]


def test_overheated_fund_is_not_recommended_for_add() -> None:
    result = build_fund_advice(
        sample_count=500,
        trend_signal="strong_up",
        return_20d=0.15,
        return_60d=0.20,
        annual_volatility=0.20,
        max_drawdown=-0.15,
        sharpe=1.2,
    )
    assert result["action"] == "hold"
    assert any("追高" in risk for risk in result["risks"])


def test_weak_fund_recommends_reduce() -> None:
    result = build_fund_advice(
        sample_count=500,
        trend_signal="strong_down",
        return_20d=-0.10,
        return_60d=-0.15,
        annual_volatility=0.35,
        max_drawdown=-0.35,
        sharpe=-0.5,
    )
    assert result["action"] in {"reduce", "reduce_more"}
    assert result["risks"]


def test_low_sample_always_downgrades_to_watch() -> None:
    result = build_fund_advice(
        sample_count=40,
        trend_signal="strong_up",
        return_20d=0.03,
        return_60d=0.08,
        annual_volatility=0.15,
        max_drawdown=-0.10,
        sharpe=1.5,
    )
    assert result["action"] == "watch"
    assert result["confidence"] == "low"
