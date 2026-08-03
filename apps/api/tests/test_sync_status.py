"""同步运行状态（sync_runs 记录与 /api/sync/status 接口）测试。"""

from datetime import date

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.sync_run import SyncRun
from app.services.sync_status import (
    VALID_STATUSES,
    derive_final_status,
    get_sync_status,
    track_sync_run,
)
from app.timezone import CN_TZ


def test_track_sync_run_success(db_session: Session) -> None:
    with track_sync_run(db_session, "fund_nav") as record:
        record(total=10, updated=10, failed=0, data_date=date(2026, 7, 30))

    run = db_session.scalar(select(SyncRun).where(SyncRun.job_name == "fund_nav"))
    assert run is not None
    assert run.status == "success"
    assert run.total == 10
    assert run.updated == 10
    assert run.failed == 0
    assert run.data_date == date(2026, 7, 30)
    assert run.started_at is not None
    assert run.finished_at is not None
    assert run.error is None
    # SQLite 回读的 tzinfo 在会话内可能不一致，统一去掉时区后比较
    assert run.finished_at.replace(tzinfo=None) >= run.started_at.replace(tzinfo=None)


def test_track_sync_run_partial_when_updated_and_failed(db_session: Session) -> None:
    """updated>0 且 failed>0 时终态必须是 partial，不能记 success。"""
    with track_sync_run(db_session, "stock_daily") as record:
        record(total=10, updated=8, failed=2)

    run = db_session.scalar(select(SyncRun).where(SyncRun.job_name == "stock_daily"))
    assert run is not None
    assert run.status == "partial"


def test_track_sync_run_failed_when_all_failed(db_session: Session) -> None:
    with track_sync_run(db_session, "stock_daily") as record:
        record(total=5, updated=0, failed=5)

    run = db_session.scalar(
        select(SyncRun).where(SyncRun.job_name == "stock_daily")
    )
    assert run is not None
    assert run.status == "failed"


def test_track_sync_run_paused_explicit(db_session: Session) -> None:
    """显式 paused 终态：任务被人为暂停/跳过。"""
    with track_sync_run(db_session, "news") as record:
        record(status="paused")

    run = db_session.scalar(select(SyncRun).where(SyncRun.job_name == "news"))
    assert run is not None
    assert run.status == "paused"
    assert "paused" in VALID_STATUSES


def test_track_sync_run_invalid_status_rejected(db_session: Session) -> None:
    with pytest.raises(ValueError, match="非法终态"):
        with track_sync_run(db_session, "news") as record:
            record(status="weird")


def test_derive_final_status() -> None:
    assert derive_final_status(8, 2) == "partial"
    assert derive_final_status(0, 2) == "failed"
    assert derive_final_status(8, 0) == "success"
    assert derive_final_status(0, 0, processed=0) == "partial"


def test_track_sync_run_failure_records_error_and_reraises(db_session: Session) -> None:
    with pytest.raises(ValueError, match="boom"):
        with track_sync_run(db_session, "news"):
            raise ValueError("boom")

    run = db_session.scalar(select(SyncRun).where(SyncRun.job_name == "news"))
    assert run is not None
    assert run.status == "failed"
    assert run.error is not None and "boom" in run.error
    assert run.finished_at is not None


def test_track_sync_run_started_at_is_beijing_time(db_session: Session) -> None:
    with track_sync_run(db_session, "indices"):
        pass
    run = db_session.scalar(select(SyncRun).where(SyncRun.job_name == "indices"))
    assert run is not None
    # 直接写入的是北京时间 aware datetime（SQLite 回读可能丢 tzinfo，
    # 这里通过服务层的换算结果验证语义）
    status = get_sync_status(db_session, job_name="indices")
    started = status["runs"][0]["started_at"]
    assert "+08:00" in started


def test_get_sync_status_groups_by_job(db_session: Session) -> None:
    for _ in range(3):
        with track_sync_run(db_session, "news") as record:
            record(total=5, updated=5, failed=0)
    with track_sync_run(db_session, "fund_nav") as record:
        record(total=2, updated=2, failed=0)

    status = get_sync_status(db_session)
    assert status["job_name"] is None
    assert "+08:00" in status["server_time"]
    job_names = {run["job_name"] for run in status["runs"]}
    assert job_names == {"news", "fund_nav"}
    # 每个任务仅返回最近一次
    assert len(status["runs"]) == 2
    news_run = next(run for run in status["runs"] if run["job_name"] == "news")
    assert news_run["status"] == "success"
    assert news_run["total"] == 5


def test_get_sync_status_by_job_name_returns_recent_runs(db_session: Session) -> None:
    for i in range(3):
        with track_sync_run(db_session, "paper") as record:
            record(total=1, updated=i, failed=0)

    status = get_sync_status(db_session, job_name="paper")
    assert status["job_name"] == "paper"
    assert len(status["runs"]) == 3
    # 倒序：最近一次在前
    assert status["runs"][0]["updated"] == 2
    assert status["runs"][0]["duration_seconds"] is not None


def test_sync_status_endpoint_summary(client: TestClient, db_session: Session) -> None:
    with track_sync_run(db_session, "holdings") as record:
        record(total=7, updated=6, failed=1)

    response = client.get("/api/sync/status")
    assert response.status_code == 200
    data = response.json()
    assert "+08:00" in data["server_time"]
    holdings = next(run for run in data["runs"] if run["job_name"] == "holdings")
    # 6 成功 1 失败 -> partial
    assert holdings["status"] == "partial"
    assert holdings["total"] == 7
    assert holdings["updated"] == 6
    assert holdings["failed"] == 1


def test_sync_status_endpoint_filter_by_job(client: TestClient, db_session: Session) -> None:
    with track_sync_run(db_session, "indices") as record:
        record(total=3, updated=3, failed=0)
    with track_sync_run(db_session, "news") as record:
        record(total=9, updated=9, failed=0)

    response = client.get("/api/sync/status", params={"job_name": "indices"})
    assert response.status_code == 200
    data = response.json()
    assert data["job_name"] == "indices"
    assert len(data["runs"]) == 1
    assert data["runs"][0]["job_name"] == "indices"


def test_sync_status_endpoint_empty(client: TestClient) -> None:
    response = client.get("/api/sync/status")
    assert response.status_code == 200
    data = response.json()
    assert data["runs"] == []
    assert data["server_time"]


def test_sync_run_model_uses_beijing_tz_constant() -> None:
    # CN_TZ 应为 Asia/Shanghai
    assert str(CN_TZ) == "Asia/Shanghai"
