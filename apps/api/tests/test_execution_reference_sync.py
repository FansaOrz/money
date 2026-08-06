"""执行参考数据持续供应、降级和 SLA 门禁测试。"""

from datetime import UTC, date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import DataSourceSLAState, IndexConstituent, QuantDataRecord
from app.services import execution_reference_sync as service


def _fetchers() -> dict[str, service.Fetcher]:
    return {
        "suspend_d": lambda _db, day: [
            {
                "ts_code": "600001",
                "trade_date": day.isoformat(),
                "suspend_type": "S",
            }
        ],
        "stk_limit": lambda _db, day: [
            {
                "ts_code": "600001",
                "trade_date": day.isoformat(),
                "up_limit": 11.0,
                "down_limit": 9.0,
            }
        ],
        "dividend": lambda _db, day: [
            {
                "ts_code": "600001",
                "ann_date": day.isoformat(),
                "ex_date": day.isoformat(),
                "cash_div": 0.1,
            }
        ],
        "namechange": lambda _db, day: [
            {
                "ts_code": "600001",
                "ann_date": day.isoformat(),
                "start_date": day.isoformat(),
                "name": "新名称",
                "old_name": "旧名称",
            }
        ],
    }


def test_refresh_persists_all_required_events_with_sla(
    db_session: Session,
) -> None:
    day = date(2026, 8, 5)
    result = service.refresh_execution_references(
        db_session,
        as_of=day,
        primary_fetchers=_fetchers(),
        fallback_fetchers={},
    )
    assert all(item["status"] == "success" for item in result["datasets"].values())
    assert db_session.query(QuantDataRecord).count() == 4
    assert db_session.query(DataSourceSLAState).count() == 4
    health = service.sla_health(db_session)
    assert all(item["ready"] for item in health.values())
    dividend = db_session.scalar(
        select(QuantDataRecord).where(QuantDataRecord.dataset == "dividend")
    )
    assert dividend is not None
    assert dividend.payload["cash_div"] == 0.1


def test_refresh_can_target_selected_datasets_without_touching_others(
    db_session: Session,
) -> None:
    result = service.refresh_execution_references(
        db_session,
        as_of=date(2026, 8, 5),
        datasets=["suspend_d"],
        primary_fetchers={"suspend_d": _fetchers()["suspend_d"]},
        fallback_fetchers={},
    )

    assert set(result["datasets"]) == {"suspend_d"}
    assert db_session.get(DataSourceSLAState, "suspend_d") is not None
    assert db_session.get(DataSourceSLAState, "stk_limit") is None


def test_refresh_rejects_unknown_selected_dataset(db_session: Session) -> None:
    try:
        service.refresh_execution_references(
            db_session,
            datasets=["not_a_dataset"],
        )
    except ValueError as exc:
        assert "not_a_dataset" in str(exc)
    else:
        raise AssertionError("未知数据集必须被拒绝")


def test_empty_event_day_does_not_invent_schema_or_block_next_event(
    db_session: Session,
) -> None:
    empty = service.refresh_execution_references(
        db_session,
        as_of=date(2026, 8, 4),
        datasets=["suspend_d"],
        primary_fetchers={"suspend_d": lambda _db, _day: []},
        fallback_fetchers={},
    )
    assert empty["datasets"]["suspend_d"]["status"] == "success"
    state = db_session.get(DataSourceSLAState, "suspend_d")
    assert state is not None and state.schema_hash is None

    event = service.refresh_execution_references(
        db_session,
        as_of=date(2026, 8, 5),
        datasets=["suspend_d"],
        primary_fetchers={
            "suspend_d": lambda _db, day: [
                {
                    "ts_code": "600001",
                    "trade_date": day.isoformat(),
                    "suspend_type": "S",
                    "reason": "重大事项",
                    "resume_date": None,
                }
            ]
        },
        fallback_fetchers={},
    )

    assert event["datasets"]["suspend_d"]["status"] == "success"
    assert state.schema_hash is not None
    assert state.row_count == 1


def test_primary_failure_uses_fallback_and_marks_degraded(
    db_session: Session,
) -> None:
    day = date(2026, 8, 5)

    def fail(_db: Session, _day: date) -> list[dict[str, object]]:
        raise RuntimeError("primary unavailable")

    primary = _fetchers()
    primary["stk_limit"] = fail
    fallback = {
        "stk_limit": lambda _db, target: [
            {
                "ts_code": "600001",
                "trade_date": target.isoformat(),
                "up_limit": 11.0,
                "down_limit": 9.0,
            }
        ]
    }
    result = service.refresh_execution_references(
        db_session,
        as_of=day,
        primary_fetchers=primary,
        fallback_fetchers=fallback,
    )
    item = result["datasets"]["stk_limit"]
    assert item["status"] == "degraded"
    state = db_session.get(DataSourceSLAState, "stk_limit")
    assert state is not None and state.degraded is True
    assert state.escalation_level == "warning"


def test_stale_or_repeated_failure_blocks_readiness(
    db_session: Session,
) -> None:
    service.initialize_policies(db_session)
    state = db_session.get(DataSourceSLAState, "suspend_d")
    assert state is not None
    state.status = "success"
    state.last_success_at = datetime.now(UTC) - timedelta(days=4)
    db_session.commit()
    health = service.sla_health(db_session)
    assert health["suspend_d"]["ready"] is False
    assert health["suspend_d"]["overdue"] is True
    assert health["stk_limit"]["status"] == "never_run"


def test_schema_change_fails_closed(db_session: Session) -> None:
    primary = _fetchers()
    primary["stk_limit"] = lambda _db, day: [
        {"ts_code": "600001", "trade_date": day.isoformat()}
    ]
    result = service.refresh_execution_references(
        db_session,
        as_of=date(2026, 8, 5),
        primary_fetchers=primary,
        fallback_fetchers={},
    )
    assert result["datasets"]["stk_limit"]["status"] == "failed"
    state = db_session.get(DataSourceSLAState, "stk_limit")
    assert state is not None and state.escalation_level == "critical"


def test_bulk_missing_limit_rows_stops_new_signals(
    db_session: Session,
) -> None:
    for index in range(10):
        db_session.add(
            IndexConstituent(
                index_code="000300",
                stock_code=f"{600000 + index:06d}",
            )
        )
    db_session.commit()
    primary = _fetchers()
    result = service.refresh_execution_references(
        db_session,
        as_of=date(2026, 8, 5),
        primary_fetchers=primary,
        fallback_fetchers={},
    )
    item = result["datasets"]["stk_limit"]
    assert item["status"] == "failed"
    assert item["safe_action"] == "reduce_only"
    assert any("批量缺失" in error for error in item["errors"])
