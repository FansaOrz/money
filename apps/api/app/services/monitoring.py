"""平台深度健康、结构化指标与告警汇总。"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
import hashlib
from pathlib import Path
import time

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import (
    BrokerAccountLedger,
    BrokerFill,
    BrokerOrder,
    DataFileManifestEntry,
    DataQualityIssue,
    DataSourceSLAState,
    PersistentJob,
    StockDailyBar,
    StockPaperAccount,
    StockPaperNavDaily,
    StockPaperPosition,
    StockPaperRun,
    SyncRun,
)


def deep_health(db: Session) -> dict[str, object]:
    from app.services.scheduler import scheduler_heartbeat_ok

    now = datetime.now(UTC)
    checks: dict[str, dict[str, object]] = {}
    database_started = time.perf_counter()
    try:
        db.execute(text("SELECT 1"))
        latency_ms = (time.perf_counter() - database_started) * 1000
        checks["database"] = {
            "ok": latency_ms <= 250,
            "latency_ms": latency_ms,
            "maximum_latency_ms": 250,
        }
    except Exception as exc:  # noqa: BLE001
        checks["database"] = {"ok": False, "detail": str(exc)}
    settings = get_settings()
    research_root = Path(settings.research_data_dir)
    checks["research_repository"] = {
        "ok": research_root.exists() and research_root.is_dir(),
        "path": str(research_root),
    }
    disk = __import__("shutil").disk_usage(research_root.parent)
    checks["disk"] = {
        "ok": disk.free / max(disk.total, 1) >= 0.10,
        "free_bytes": disk.free,
        "total_bytes": disk.total,
        "minimum_free_ratio": 0.10,
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
    total_symbols = int(db.scalar(select(func.count(StockDailyBar.code))) or 0)
    fresh_symbols = int(
        db.scalar(
            select(func.count(StockDailyBar.code)).where(
                StockDailyBar.last_trade_date == latest_daily
            )
        )
        or 0
    )
    coverage = fresh_symbols / total_symbols if total_symbols else 0.0
    checks["stock_cross_section_coverage"] = {
        "ok": coverage >= 0.95,
        "fresh_symbols": fresh_symbols,
        "total_symbols": total_symbols,
        "coverage": coverage,
        "minimum": 0.95,
    }
    sla_rows = list(
        db.scalars(
            select(DataSourceSLAState).where(DataSourceSLAState.required.is_(True))
        ).all()
    )
    stale_datasets = [
        row.dataset
        for row in sla_rows
        if row.status != "success"
        or row.last_success_at is None
        or now - row.last_success_at
        > timedelta(minutes=row.max_latency_minutes)
    ]
    checks["critical_datasets"] = {
        "ok": not stale_datasets,
        "stale_or_failed": stale_datasets,
        "required_count": len(sla_rows),
    }
    manifest_rows = list(
        db.scalars(
            select(DataFileManifestEntry)
            .order_by(DataFileManifestEntry.frozen_at.desc())
            .limit(20)
        ).all()
    )
    hash_failures: list[str] = []
    for row in manifest_rows:
        path = research_root / row.relative_path
        if not path.is_file() or path.stat().st_size != row.size_bytes:
            hash_failures.append(row.relative_path)
            continue
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        if digest.hexdigest() != row.file_sha256:
            hash_failures.append(row.relative_path)
    checks["file_manifest"] = {
        "ok": bool(manifest_rows) and not hash_failures,
        "sampled": len(manifest_rows),
        "missing_or_size_mismatch": hash_failures,
    }
    try:
        migration = db.execute(text("SELECT version_num FROM alembic_version")).scalar()
    except Exception:
        migration = None
    checks["database_migration"] = {
        "ok": migration is not None,
        "revision": migration,
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
        "ok": (
            stale_jobs == 0
            and failed_jobs == 0
            and scheduler_heartbeat_ok()
        ),
        "stale_running": int(stale_jobs),
        "failed_24h": int(failed_jobs),
        "heartbeat_ok": scheduler_heartbeat_ok(),
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
    latest_nav = (
        db.scalar(
            select(StockPaperNavDaily)
            .where(StockPaperNavDaily.account_id == account.id)
            .order_by(StockPaperNavDaily.nav_date.desc())
            .limit(1)
        )
        if account is not None
        else None
    )
    broker_breaks = int(
        db.scalar(
            select(func.count(BrokerAccountLedger.account)).where(
                BrokerAccountLedger.reconciliation_status != "clean"
            )
        )
        or 0
    )
    orphan_fills = int(
        db.scalar(
            select(func.count(BrokerFill.id))
            .outerjoin(BrokerOrder, BrokerFill.order_id == BrokerOrder.id)
            .where(BrokerOrder.id.is_(None))
        )
        or 0
    )
    negative_positions = int(
        db.scalar(
            select(func.count(StockPaperPosition.id)).where(
                StockPaperPosition.shares < 0
            )
        )
        or 0
    )
    invalid_nav_or_benchmark = int(
        db.scalar(
            select(func.count(StockPaperNavDaily.id)).where(
                (StockPaperNavDaily.nav <= 0)
                | (StockPaperNavDaily.benchmark_nav <= 0)
            )
        )
        or 0
    )
    checks["ledger_continuity"] = {
        "ok": (
            broker_breaks == 0
            and orphan_fills == 0
            and negative_positions == 0
            and invalid_nav_or_benchmark == 0
            and (
            account is None
            or latest_nav is not None
            and abs(float(latest_nav.cash_conservation_error)) <= 0.01
            )
        ),
        "broker_reconciliation_breaks": broker_breaks,
        "orphan_fills": orphan_fills,
        "negative_positions": negative_positions,
        "invalid_nav_or_benchmark": invalid_nav_or_benchmark,
        "latest_cash_conservation_error": (
            float(latest_nav.cash_conservation_error)
            if latest_nav is not None
            else None
        ),
    }
    backups_root = research_root.parent / "backups"
    manifests = list(backups_root.glob("*/manifest.json")) if backups_root.exists() else []
    newest_backup_age = (
        min((now.timestamp() - path.stat().st_mtime for path in manifests))
        if manifests
        else None
    )
    checks["backup_age"] = {
        "ok": newest_backup_age is not None and newest_backup_age <= 36 * 3600,
        "age_seconds": newest_backup_age,
        "maximum_seconds": 36 * 3600,
    }
    from app.services.time_health import time_health

    checks["clock"] = time_health()
    checks["clock"]["ok"] = checks["clock"].pop("status") == "ok"
    ok = all(bool(item.get("ok")) for item in checks.values())
    failures = sum(not bool(item.get("ok")) for item in checks.values())
    return {
        "status": "ok" if ok else "failed" if failures >= 3 else "degraded",
        "checks": checks,
    }


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
