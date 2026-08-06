"""跨源异常降级、差异阻断与人工复核测试。"""

from datetime import date

import pytest
from sqlalchemy.orm import Session

from app.models import DataQualityIssue, DataSourceReconciliation
from app.services.source_reconciliation import (
    SourceConflictError,
    reconcile_field,
    resolve_review,
    selected_value,
)


def test_abnormal_primary_degrades_to_valid_fallback(
    db_session: Session,
) -> None:
    decision = reconcile_field(
        db_session,
        dataset="daily_price",
        code="600001",
        effective_date=date(2026, 8, 5),
        field_name="close",
        candidates=[("tushare", -1.0), ("sina", 10.0)],
        threshold=0.002,
    )
    assert decision.status == "degraded"
    assert decision.selected_source == "sina"
    assert selected_value(decision) == 10.0
    assert db_session.query(DataQualityIssue).count() == 1


def test_material_difference_blocks_until_reviewed(
    db_session: Session,
) -> None:
    decision = reconcile_field(
        db_session,
        dataset="valuation",
        code="600001",
        effective_date=date(2026, 8, 5),
        field_name="pb",
        candidates=[("tushare", 1.0), ("baidu", 2.0)],
        threshold=0.02,
    )
    assert decision.status == "review_required"
    with pytest.raises(SourceConflictError):
        selected_value(decision)
    resolved = resolve_review(
        db_session,
        decision,
        selected_source="baidu",
        selected_value_override=2.0,
        actor="reviewer",
        rationale="核对公告后采用备用源",
    )
    assert resolved.status == "resolved"
    assert selected_value(resolved) == 2.0
    assert db_session.query(DataSourceReconciliation).count() == 1


def test_roe_uses_percentage_units_for_plausibility(db_session: Session) -> None:
    matched = reconcile_field(
        db_session,
        dataset="financial",
        code="600001",
        effective_date=date(2026, 3, 31),
        field_name="roe",
        candidates=[("tushare", 45.29), ("sina", 45.29)],
        threshold=0.02,
    )

    assert matched.status == "matched"
    assert selected_value(matched) == 45.29


def test_optional_pe_missing_does_not_block_reconciliation(
    db_session: Session,
) -> None:
    decision = reconcile_field(
        db_session,
        dataset="valuation",
        code="600001",
        effective_date=date(2026, 8, 5),
        field_name="pe_ttm",
        candidates=[("tushare", None), ("tencent", None)],
        optional_if_all_missing=True,
    )

    assert decision.status == "optional_missing"
    assert decision.selected_source is None
    assert db_session.query(DataQualityIssue).count() == 0


def test_taxonomy_divergence_keeps_primary_and_only_warns(
    db_session: Session,
) -> None:
    decision = reconcile_field(
        db_session,
        dataset="industry_classification",
        code="600001",
        effective_date=date(2026, 8, 5),
        field_name="industry_name",
        candidates=[
            ("stocktoday_sw2021", "电子"),
            ("cninfo:taxonomy_crosswalk:安防设备", "计算机"),
        ],
        categorical_mismatch_status="taxonomy_divergence",
    )

    assert decision.status == "taxonomy_divergence"
    assert decision.selected_source == "stocktoday_sw2021"
    assert selected_value(decision) == "电子"
    issue = db_session.query(DataQualityIssue).one()
    assert issue.severity == "warning"
