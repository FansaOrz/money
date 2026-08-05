"""实验预注册、失败留存、有效尝试数和禁止删除测试。"""

import pytest
from sqlalchemy.orm import Session

from app.services.experiment_registry import (
    attempt_statistics,
    preregister_experiment,
    record_trial,
    start_experiment,
)


def test_failed_trials_are_counted_and_cannot_be_deleted(
    db_session: Session,
) -> None:
    experiment = preregister_experiment(
        db_session,
        experiment_key="EXP-IMMUTABLE-001",
        hypothesis="价值因子在样本外有正 IC",
        parameter_space={"weight": [0.1, 0.2]},
        target_metrics=["rank_ic"],
        data_scope={"start": "2020-01-01", "end": "2024-12-31"},
        actor="tester",
    )
    start_experiment(db_session, experiment.id)
    failed = record_trial(
        db_session,
        experiment_id=experiment.id,
        trial_key="trial-failed",
        factor_spec={"factor": "value"},
        parameters={"weight": 0.1},
        status="failed",
        error="expected test failure",
        score_series=[0.1, 0.2, 0.3],
    )
    record_trial(
        db_session,
        experiment_id=experiment.id,
        trial_key="trial-ok",
        factor_spec={"factor": "value"},
        parameters={"weight": 0.2},
        status="completed",
        metrics={"rank_ic": 0.01},
        score_series=[0.1, 0.2, 0.3],
    )
    stats = attempt_statistics(db_session, experiment.id)
    assert stats["total_attempts"] == 2
    assert stats["failed"] == 1
    assert stats["effective_attempts"] < 2
    with pytest.raises(ValueError, match="不可删除"):
        db_session.delete(failed)
        db_session.flush()
    db_session.rollback()


def test_trial_cannot_run_without_preregistration(db_session: Session) -> None:
    with pytest.raises(ValueError, match="预注册"):
        record_trial(
            db_session,
            experiment_id=999999,
            trial_key="illegal",
            factor_spec={},
            parameters={},
            status="failed",
        )
