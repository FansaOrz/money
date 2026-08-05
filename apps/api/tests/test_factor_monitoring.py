"""持续失效、容量、拥挤和结构突变的自动动作测试。"""

from datetime import date

from app.services.factor_monitoring import FactorPeriodMetric, monitor_factor


def _metric(month: int, ic: float, **overrides) -> FactorPeriodMetric:
    return FactorPeriodMetric(
        as_of=date(2025 + (month - 1) // 12, (month - 1) % 12 + 1, 1),
        rank_ic=ic,
        top_minus_bottom_return=ic / 10,
        turnover=overrides.get("turnover", 0.3),
        capacity_ratio=overrides.get("capacity_ratio", 0.4),
        maximum_peer_correlation=overrides.get("correlation", 0.5),
        exposure=overrides.get("exposure", 0.1),
    )


def test_six_consecutive_negative_ic_periods_pause_factor() -> None:
    history = [_metric(month, 0.02) for month in range(1, 7)]
    history += [_metric(month, -0.03) for month in range(7, 13)]
    decision = monitor_factor("value", history)
    assert decision.action == "pause"
    assert decision.weight_multiplier == 0.0


def test_capacity_and_crowding_trigger_downgrade() -> None:
    history = [_metric(month, 0.02) for month in range(1, 12)]
    history.append(_metric(12, 0.02, capacity_ratio=0.9, correlation=0.95))
    decision = monitor_factor("momentum", history)
    assert decision.action in {"downweight", "retrain_review"}
    assert decision.weight_multiplier <= 0.5
