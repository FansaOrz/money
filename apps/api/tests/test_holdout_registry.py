"""留出区间一经查看即永久消耗。"""

import hashlib
from datetime import UTC, date, datetime

import pytest

from app.models import ResearchExperiment
from app.services import holdout_registry


def _experiment(db_session, key: str) -> ResearchExperiment:
    row = ResearchExperiment(
        experiment_key=key,
        hypothesis="test",
        parameter_space={},
        target_metrics=[],
        data_scope={},
        status="registered",
        registered_by="tester",
        registered_at=datetime.now(UTC),
        result_summary={},
        registration_sha256=hashlib.sha256(key.encode()).hexdigest(),
    )
    db_session.add(row)
    db_session.flush()
    return row


def test_second_experiment_cannot_call_same_interval_pristine(db_session) -> None:
    first = _experiment(db_session, "holdout-first")
    second = _experiment(db_session, "holdout-second")
    holdout_registry.assert_pristine(
        db_session, date(2025, 1, 1), date(2025, 12, 31)
    )
    consumed = holdout_registry.consume(
        db_session,
        experiment_id=first.id,
        strategy_version_id=None,
        interval_start=date(2025, 1, 1),
        interval_end=date(2025, 12, 31),
        purpose="final evaluation",
        result_sha256="a" * 64,
        actor="tester",
    )
    assert consumed.status == "pristine_consumed"
    with pytest.raises(ValueError, match="永久消耗"):
        holdout_registry.assert_pristine(
            db_session, date(2025, 1, 1), date(2025, 12, 31)
        )
    reused = holdout_registry.consume(
        db_session,
        experiment_id=second.id,
        strategy_version_id=None,
        interval_start=date(2025, 1, 1),
        interval_end=date(2025, 12, 31),
        purpose="diagnostic only",
        result_sha256="b" * 64,
        actor="tester",
    )
    assert reused.status == "contaminated_reuse"
