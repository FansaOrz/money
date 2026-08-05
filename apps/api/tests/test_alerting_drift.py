"""外部告警生命周期与模型/数据漂移动作。"""

from app.services import alerting
from app.services.model_drift import classify_drift


def test_alert_dedup_and_recovery(db_session) -> None:
    delivered: list[dict[str, object]] = []
    kwargs = {
        "dedup_key": "api-down",
        "severity": "critical",
        "strategy": "stock",
        "account": "paper",
        "impact": "API unavailable",
        "correlation_id": "corr",
        "action_url": "https://ops/runbook",
        "sender": delivered.append,
    }
    first = alerting.emit_alert(db_session, **kwargs)
    repeated = alerting.emit_alert(db_session, **kwargs)
    assert repeated.id == first.id
    assert len(delivered) == 1
    alerting.recover_alert(
        db_session, dedup_key="api-down", sender=delivered.append
    )
    assert delivered[-1]["event"] == "recovery"


def test_constant_coverage_drop_and_negative_ic_stop() -> None:
    result = classify_drift(
        baseline_features=list(range(100)),
        current_features=[1.0] * 100,
        feature_coverage=0.5,
        rolling_ic=[-0.03, -0.04, -0.05],
        return_change=-1,
        turnover_change=0,
        cost_change=0,
        exposure_change=0,
        market_volatility_change=0,
    )
    assert result["data_drift"] is True
    assert result["model_drift"] is True
    assert result["action"] == "stop"
