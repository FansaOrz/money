"""数据库持久化任务队列：幂等入队、租约锁、重试、恢复与检查点。"""

from __future__ import annotations

import socket
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.models import PersistentJob


def enqueue(
    db: Session,
    job_name: str,
    scheduled_for: datetime,
    *,
    depends_on: list[str] | None = None,
    payload: dict | None = None,
    max_attempts: int = 3,
) -> PersistentJob:
    existing = db.scalar(
        select(PersistentJob).where(
            PersistentJob.job_name == job_name,
            PersistentJob.scheduled_for == scheduled_for,
        )
    )
    if existing is not None:
        return existing
    job = PersistentJob(
        job_name=job_name,
        scheduled_for=scheduled_for,
        status="queued",
        attempt=0,
        max_attempts=max_attempts,
        depends_on=depends_on or [],
        payload=payload or {},
        checkpoint={},
        correlation_id=uuid.uuid4().hex,
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    return job


def recover_expired(db: Session, now: datetime | None = None) -> int:
    now = now or datetime.now(UTC)
    rows = db.scalars(
        select(PersistentJob).where(
            PersistentJob.status == "running",
            PersistentJob.locked_until < now,
        )
    ).all()
    for row in rows:
        row.status = "queued" if row.attempt < row.max_attempts else "failed"
        row.locked_by = None
        row.locked_until = None
        row.error = "worker 租约过期，已恢复" if row.status == "queued" else "重试耗尽"
    db.commit()
    return len(rows)


def claim(
    db: Session,
    *,
    now: datetime | None = None,
    lease_seconds: int = 3600,
    worker_id: str | None = None,
) -> PersistentJob | None:
    now = now or datetime.now(UTC)
    worker_id = worker_id or f"{socket.gethostname()}:{uuid.uuid4().hex[:8]}"
    recover_expired(db, now)
    candidates = db.scalars(
        select(PersistentJob)
        .where(
            PersistentJob.status == "queued",
            PersistentJob.scheduled_for <= now,
            or_(
                PersistentJob.locked_until.is_(None),
                PersistentJob.locked_until < now,
            ),
        )
        .order_by(PersistentJob.scheduled_for, PersistentJob.id)
    ).all()
    for job in candidates:
        dependencies = set(job.depends_on or [])
        if dependencies:
            completed = set(
                db.scalars(
                    select(PersistentJob.job_name).where(
                        PersistentJob.job_name.in_(dependencies),
                        PersistentJob.status == "success",
                        PersistentJob.scheduled_for <= job.scheduled_for,
                    )
                ).all()
            )
            if not dependencies.issubset(completed):
                continue
        job.status = "running"
        job.attempt += 1
        job.locked_by = worker_id
        job.locked_until = now + timedelta(seconds=lease_seconds)
        db.commit()
        db.refresh(job)
        return job
    return None


def checkpoint(db: Session, job: PersistentJob, value: dict) -> None:
    job.checkpoint = value
    db.commit()


def complete(db: Session, job: PersistentJob) -> None:
    job.status = "success"
    job.finished_at = datetime.now(UTC)
    job.locked_by = None
    job.locked_until = None
    job.error = None
    db.commit()


def fail(db: Session, job: PersistentJob, error: str) -> None:
    job.error = error[:4000]
    job.locked_by = None
    job.locked_until = None
    if job.attempt < job.max_attempts:
        job.status = "queued"
        job.scheduled_for = datetime.now(UTC) + timedelta(
            minutes=min(2 ** job.attempt, 30)
        )
    else:
        job.status = "failed"
        job.finished_at = datetime.now(UTC)
    db.commit()
