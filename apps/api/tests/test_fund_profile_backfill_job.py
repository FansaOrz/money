"""active 基金画像批量回填 CLI 测试。"""

from datetime import datetime, timedelta

import pytest
from sqlalchemy.orm import Session

from app.models import FundCatalogEntry, FundProfile
from app.services import fund_profile_backfill_job as job


def _seed_catalog(db: Session) -> None:
    db.add_all(
        [
            FundCatalogEntry(code="110022", name="易方达消费行业股票", fund_type="股票型", active=True),
            FundCatalogEntry(code="000001", name="华夏成长混合", fund_type="混合型", active=True),
            FundCatalogEntry(code="519736", name="交银新成长混合", fund_type="混合型", active=True),
            FundCatalogEntry(code="999999", name="已清盘基金", fund_type="股票型", active=False),
        ]
    )
    db.commit()


@pytest.fixture()
def patched_run(db_session: Session, monkeypatch: pytest.MonkeyPatch) -> dict[str, list[str]]:
    """替换 SessionLocal 与 sync_profile，记录调用代码。"""
    calls: dict[str, list[str]] = {"synced": []}
    monkeypatch.setattr(job, "SessionLocal", lambda: db_session)
    monkeypatch.setattr(job, "create_tables", lambda: None)

    def fake_sync(db: Session, code: str) -> tuple[FundProfile, list[str]]:
        calls["synced"].append(code)
        record = db.get(FundProfile, code) or FundProfile(code=code)
        record.source = "test"
        record.last_error = None
        record.fetched_at = datetime.now()
        db.add(record)
        db.commit()
        return record, []

    monkeypatch.setattr(job, "sync_profile", fake_sync)
    return calls


def test_active_filter_and_never_resume(db_session: Session, patched_run) -> None:
    """默认只选 active 且从未抓取的基金，已清盘与已有成功画像的被跳过。"""
    _seed_catalog(db_session)
    db_session.add(
        FundProfile(code="000001", source="test", last_error=None, fetched_at=datetime.now())
    )
    db_session.commit()

    summary = job.main(["--interval", "0", "--limit", "2"])

    assert summary["total"] == 2
    assert summary["ok"] == 2
    # 000001 已有成功画像、999999 非 active，本批只处理两只 never
    assert sorted(patched_run["synced"]) == ["110022", "519736"]
    # limit 放宽后，成功画像会作为 stale 按最旧补位
    summary = job.main(["--interval", "0"])
    assert summary["total"] == 3
    states = {item["code"]: item["state"] for item in summary["items"]}
    assert states["000001"] == "stale"


def test_error_state_retried_before_stale(db_session: Session, patched_run) -> None:
    """上轮失败的基金优先重试，成功的按最旧补位。"""
    _seed_catalog(db_session)
    db_session.add_all(
        [
            FundProfile(code="000001", source=None, last_error="xq down", fetched_at=datetime.now()),
            FundProfile(
                code="110022",
                source="test",
                last_error=None,
                fetched_at=datetime.now() - timedelta(days=40),
            ),
            FundProfile(
                code="519736",
                source="test",
                last_error=None,
                fetched_at=datetime.now() - timedelta(days=10),
            ),
        ]
    )
    db_session.commit()

    summary = job.main(["--interval", "0", "--limit", "2"])

    # error 优先，其次最旧（110022 比 519736 旧）
    assert patched_run["synced"] == ["000001", "110022"]
    assert summary["ok"] == 2


def test_force_reprocesses_all_active(db_session: Session, patched_run) -> None:
    """--force 忽略画像状态，重抓全部 active（受 limit 限制）。"""
    _seed_catalog(db_session)
    db_session.add(
        FundProfile(code="000001", source="test", last_error=None, fetched_at=datetime.now())
    )
    db_session.commit()

    summary = job.main(["--interval", "0", "--force", "--limit", "3"])

    assert summary["total"] == 3
    assert patched_run["synced"] == ["000001", "110022", "519736"]


def test_codes_intersect_active(db_session: Session, patched_run) -> None:
    """--codes 与 active 取交集，非 active 代码被排除。"""
    _seed_catalog(db_session)

    summary = job.main(["--interval", "0", "--codes", "110022,999999,123456"])

    assert patched_run["synced"] == ["110022"]
    assert summary["total"] == 1
    assert summary["active_total"] == 1


def test_dry_run_makes_no_requests(db_session: Session, patched_run, capsys) -> None:
    """--dry-run 只打印计划，不发起任何抓取。"""
    _seed_catalog(db_session)

    summary = job.main(["--interval", "0", "--dry-run", "--limit", "2"])

    assert summary["dry_run"] is True
    assert summary["batch"] == 2
    assert patched_run["synced"] == []
    out = capsys.readouterr().out
    assert "[dry-run] 000001" in out
    assert "[dry-run] 110022" in out


def test_failure_continues_and_summary_counts(db_session: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    """单只失败不中断，错误写入画像并计入 summary。"""
    _seed_catalog(db_session)
    monkeypatch.setattr(job, "SessionLocal", lambda: db_session)
    monkeypatch.setattr(job, "create_tables", lambda: None)

    def flaky_sync(db: Session, code: str) -> tuple[FundProfile, list[str]]:
        record = db.get(FundProfile, code) or FundProfile(code=code)
        if code == "110022":
            record.last_error = "雪球与东财均失败"
            record.fetched_at = datetime.now()
            db.add(record)
            db.commit()
            return record, ["基金介绍暂不可用"]
        record.source = "test"
        record.last_error = None
        record.fetched_at = datetime.now()
        db.add(record)
        db.commit()
        return record, []

    monkeypatch.setattr(job, "sync_profile", flaky_sync)

    summary = job.main(["--interval", "0"])

    assert summary["total"] == 3
    assert summary["ok"] == 2
    assert summary["failed"] == 1
    failed = [item for item in summary["items"] if item["status"] == "failed"]
    assert failed[0]["code"] == "110022"
    assert "雪球" in failed[0]["error"]
    # 失败记录落库，便于下次断点重试
    assert db_session.get(FundProfile, "110022").last_error
