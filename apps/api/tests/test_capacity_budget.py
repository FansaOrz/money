"""容量预算会持久化且超预算明确失败。"""

from app.services.capacity_budget import measured_run, record_metric


def test_runtime_and_resource_budget_status(db_session) -> None:
    result, metric = measured_run(
        db_session,
        metric_name="signal_generation_seconds",
        operation=lambda: 42,
        budget_seconds=1.0,
    )
    assert result == 42
    assert metric.status == "within_budget"
    exceeded = record_metric(
        db_session,
        metric_name="api_latency_ms",
        value=1000,
        unit="ms",
    )
    assert exceeded.status == "budget_exceeded"
