"""生产容量、性能、资源预算和交易前 SLA 证据。"""

from __future__ import annotations

import os
import resource
import shutil
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import TypeVar

from sqlalchemy.orm import Session

from app.models import OperationalMetric

T = TypeVar("T")

DEFAULT_BUDGETS = {
    "api_latency_ms": 500.0,
    "backtest_runtime_seconds": 3_600.0,
    "signal_generation_seconds": 900.0,
    "database_size_bytes": 100 * 1024**3,
    "disk_growth_bytes_per_day": 5 * 1024**3,
    "paper_cycle_finish_before_open_minutes": 30.0,
    "memory_peak_mb": 4096.0,
}


def record_metric(
    db: Session,
    *,
    metric_name: str,
    value: float,
    unit: str,
    labels: dict[str, object] | None = None,
    budget: float | None = None,
) -> OperationalMetric:
    threshold = (
        float(budget)
        if budget is not None
        else DEFAULT_BUDGETS.get(metric_name)
    )
    status = (
        "within_budget"
        if threshold is None or value <= threshold
        else "budget_exceeded"
    )
    row = OperationalMetric(
        metric_name=metric_name,
        value=value,
        unit=unit,
        labels=labels or {},
        budget={"maximum": threshold},
        status=status,
        observed_at=datetime.now(UTC),
    )
    db.add(row)
    db.flush()
    return row


def measured_run(
    db: Session,
    *,
    metric_name: str,
    operation: Callable[[], T],
    budget_seconds: float,
    labels: dict[str, object] | None = None,
) -> tuple[T, OperationalMetric]:
    started = time.monotonic()
    before_memory = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    result = operation()
    elapsed = time.monotonic() - started
    after_memory = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    metric = record_metric(
        db,
        metric_name=metric_name,
        value=elapsed,
        unit="seconds",
        labels={
            **(labels or {}),
            "memory_peak_delta_kb": max(after_memory - before_memory, 0),
        },
        budget=budget_seconds,
    )
    return result, metric


def resource_snapshot(
    db: Session,
    *,
    database_path: Path | None,
    data_path: Path,
) -> dict[str, object]:
    disk = shutil.disk_usage(data_path)
    database_size = (
        database_path.stat().st_size
        if database_path is not None and database_path.exists()
        else 0
    )
    memory_mb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0
    rows = [
        record_metric(
            db,
            metric_name="database_size_bytes",
            value=float(database_size),
            unit="bytes",
        ),
        record_metric(
            db,
            metric_name="memory_peak_mb",
            value=memory_mb,
            unit="MB",
        ),
    ]
    return {
        "database_size_bytes": database_size,
        "disk_total_bytes": disk.total,
        "disk_free_bytes": disk.free,
        "memory_peak_mb": memory_mb,
        "pid": os.getpid(),
        "status": (
            "within_budget"
            if all(row.status == "within_budget" for row in rows)
            and disk.free / max(disk.total, 1) >= 0.10
            else "budget_exceeded"
        ),
    }
