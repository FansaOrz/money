"""质量异常落库、修正证据与原始数据不可变测试。"""

from datetime import UTC, date, datetime

from sqlalchemy.orm import Session

from app.models import DataCorrection, DataQualityIssue, QuantDataRecord
from app.services.data_quality_scan import scan_quant_records
from app.services.quant_data_governance import apply_correction


def test_injected_anomaly_creates_issue_and_correction_without_overwrite(
    db_session: Session,
) -> None:
    payload = {
        "trade_date": "20260805",
        "close": 10.0,
        "total_mv": 1000.0,
        "pb": 9999.0,
    }
    record = QuantDataRecord(
        dataset="daily_basic",
        code="600001",
        effective_date=date(2026, 8, 5),
        available_at=datetime(2026, 8, 5, tzinfo=UTC),
        source="test",
        source_file="immutable.parquet",
        source_hash="a" * 64,
        payload=dict(payload),
        imported_at=datetime.now(UTC),
    )
    db_session.add(record)
    db_session.commit()
    result = scan_quant_records(db_session, [record])
    assert result["issues_created"] == 1
    issue = db_session.query(DataQualityIssue).one()
    assert issue.rule == "economic_extreme"
    assert issue.field_name == "pb"
    correction = apply_correction(
        db_session,
        issue,
        corrected_value=None,
        correction_rule="QUARANTINE_FIELD_V1",
        actor="quality-test",
        affected_strategy_versions=[7, 8],
    )
    db_session.refresh(record)
    assert record.payload == payload
    assert db_session.query(DataCorrection).count() == 1
    assert correction.affected_strategy_versions == [7, 8]
    assert len(correction.evidence_sha256) == 64
    assert issue.status == "resolved"
