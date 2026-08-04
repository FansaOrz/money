"""平台深度健康、结构化指标与告警汇总。"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import (
    DataQualityIssue,
    PersistentJob,
    StockDailyBar,
    StockPaperAccount,
    StockPaperRun,
    SyncRun,
)


def deep_health(db: Session) -> dict[str, object]:
    now = datetime.now(UTC)
    checks: dict[str, dict[str, object]] = {}
    try:
        db.execute(text("SELECT 1"))
        checks["database"] = {"ok": True}
    except Exception as exc:  # noqa: BLE001
        checks["database"] = {"ok": False, "detail": str(exc)}
    settings = get_settings()
    research_root = Path(settings.research_data_dir)
    checks["research_repository"] = {
        "ok": research_root.exists() and research_root.is_dir(),
        "path": str(research_root),
    }
    latest_daily = db.scalar(select(func.max(StockDailyBar.last_trade_date)))
    stale_days = (
        max((date.today() - latest_daily).days, 0)
        if latest_daily is not None
        else None
    )
    checks["stock_freshness"] = {
        "ok": stale_days is not None and stale_days <= 7,
        "latest_data_date": latest_daily.isoformat() if latest_daily else None,
        "stale_calendar_days": stale_days,
        "threshold_calendar_days": 7,
    }
    stale_jobs = db.scalar(
        select(func.count(PersistentJob.id)).where(
            PersistentJob.status == "running",
            PersistentJob.locked_until < now,
        )
    ) or 0
    failed_jobs = db.scalar(
        select(func.count(PersistentJob.id)).where(
            PersistentJob.status == "failed",
            PersistentJob.finished_at >= now - timedelta(days=1),
        )
    ) or 0
    checks["scheduler"] = {
        "ok": stale_jobs == 0 and failed_jobs == 0,
        "stale_running": int(stale_jobs),
        "failed_24h": int(failed_jobs),
    }
    account = db.scalar(
        select(StockPaperAccount).order_by(StockPaperAccount.id.desc()).limit(1)
    )
    latest_run = (
        db.scalar(
            select(StockPaperRun)
            .where(StockPaperRun.account_id == account.id)
            .order_by(StockPaperRun.run_date.desc())
            .limit(1)
        )
        if account is not None
        else None
    )
    checks["stock_paper"] = {
        "ok": account is None or latest_run is not None,
        "account_status": account.status if account else "not_started",
        "latest_run": latest_run.run_date.isoformat() if latest_run else None,
    }
    ok = all(bool(item.get("ok")) for item in checks.values())
    return {"status": "ok" if ok else "degraded", "checks": checks}


def metrics(db: Session) -> dict[str, int]:
    now = datetime.now(UTC)
    return {
        "persistent_jobs_queued": int(
            db.scalar(
                select(func.count(PersistentJob.id)).where(
                    PersistentJob.status == "queued"
                )
            )
            or 0
        ),
        "persistent_jobs_failed_24h": int(
            db.scalar(
                select(func.count(PersistentJob.id)).where(
                    PersistentJob.status == "failed",
                    PersistentJob.finished_at >= now - timedelta(days=1),
                )
            )
            or 0
        ),
        "sync_runs_failed_24h": int(
            db.scalar(
                select(func.count(SyncRun.id)).where(
                    SyncRun.status == "failed",
                    SyncRun.started_at >= now - timedelta(days=1),
                )
            )
            or 0
        ),
        "sync_runs_partial_24h": int(
            db.scalar(
                select(func.count(SyncRun.id)).where(
                    SyncRun.status == "partial",
                    SyncRun.started_at >= now - timedelta(days=1),
                )
            )
            or 0
        ),
        "data_quality_open": int(
            db.scalar(
                select(func.count(DataQualityIssue.id)).where(
                    DataQualityIssue.status == "open"
                )
            )
            or 0
        ),
    }
