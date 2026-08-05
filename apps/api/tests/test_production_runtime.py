"""生产数据库、任务租约和调度心跳的 fail-closed 规则。"""

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from app.config import Settings
from app.services import job_queue, scheduler


def test_production_rejects_sqlite() -> None:
    with pytest.raises(ValidationError, match="PostgreSQL"):
        Settings(
            environment="production",
            database_url="sqlite:///money.db",
        )


def test_duplicate_trigger_crash_and_lease_recovery_are_idempotent(
    db_session,
) -> None:
    scheduled = datetime.now(UTC) - timedelta(minutes=1)
    first = job_queue.enqueue(db_session, "pressure-job", scheduled)
    repeated = job_queue.enqueue(db_session, "pressure-job", scheduled)
    assert repeated.id == first.id
    claimed = job_queue.claim(
        db_session,
        now=datetime.now(UTC),
        lease_seconds=1,
        worker_id="worker-a",
    )
    assert claimed.id == first.id
    job_queue.checkpoint(db_session, claimed, {"offset": 100})
    claimed.locked_until = datetime.now(UTC) - timedelta(seconds=1)
    db_session.commit()
    assert job_queue.recover_expired(db_session) == 1
    reclaimed = job_queue.claim(
        db_session,
        now=datetime.now(UTC),
        worker_id="worker-b",
    )
    assert reclaimed.id == first.id
    assert reclaimed.checkpoint == {"offset": 100}


def test_scheduler_heartbeat_is_deep_not_pid_only(tmp_path, monkeypatch) -> None:
    heartbeat = tmp_path / "heartbeat"
    monkeypatch.setattr(scheduler, "_HEARTBEAT_PATH", heartbeat)
    assert scheduler.scheduler_heartbeat_ok() is False
    heartbeat.write_text("ok", encoding="utf-8")
    assert scheduler.scheduler_heartbeat_ok() is True
