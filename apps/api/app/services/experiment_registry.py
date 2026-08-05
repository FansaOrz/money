"""不可删除的实验预注册、尝试账本与有效多重检验数量。"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime

import numpy as np
from sqlalchemy import select

from app.models import ResearchExperiment, ResearchTrialAttempt


def _hash(payload: object) -> str:
    return hashlib.sha256(
        json.dumps(
            payload, ensure_ascii=False, sort_keys=True, default=str
        ).encode()
    ).hexdigest()


def preregister_experiment(
    db: object,
    *,
    experiment_key: str,
    hypothesis: str,
    parameter_space: dict[str, object],
    target_metrics: list[str],
    data_scope: dict[str, object],
    actor: str,
) -> ResearchExperiment:
    if not hypothesis.strip() or not target_metrics:
        raise ValueError("实验必须预先声明假设和目标指标")
    existing = db.scalar(  # type: ignore[attr-defined]
        select(ResearchExperiment).where(
            ResearchExperiment.experiment_key == experiment_key
        )
    )
    if existing is not None:
        return existing
    payload = {
        "experiment_key": experiment_key,
        "hypothesis": hypothesis,
        "parameter_space": parameter_space,
        "target_metrics": target_metrics,
        "data_scope": data_scope,
        "actor": actor,
    }
    row = ResearchExperiment(
        experiment_key=experiment_key,
        hypothesis=hypothesis,
        parameter_space=parameter_space,
        target_metrics=target_metrics,
        data_scope=data_scope,
        status="registered",
        registered_by=actor,
        registered_at=datetime.now(UTC),
        result_summary={},
        registration_sha256=_hash(payload),
    )
    db.add(row)  # type: ignore[attr-defined]
    db.flush()  # type: ignore[attr-defined]
    return row


def start_experiment(db: object, experiment_id: int) -> ResearchExperiment:
    row = db.get(ResearchExperiment, experiment_id)  # type: ignore[attr-defined]
    if row is None:
        raise ValueError("实验不存在")
    if row.status != "registered":
        raise ValueError(f"实验状态 {row.status} 不允许启动")
    row.status = "running"
    row.started_at = datetime.now(UTC)
    db.flush()  # type: ignore[attr-defined]
    return row


def record_trial(
    db: object,
    *,
    experiment_id: int,
    trial_key: str,
    factor_spec: dict[str, object],
    parameters: dict[str, object],
    status: str,
    metrics: dict[str, object] | None = None,
    score_series: list[float] | None = None,
    error: str | None = None,
) -> ResearchTrialAttempt:
    experiment = db.get(ResearchExperiment, experiment_id)  # type: ignore[attr-defined]
    if experiment is None or experiment.status != "running":
        raise ValueError("只能向已预注册并启动的实验写入尝试")
    if status not in {"completed", "failed", "abandoned"}:
        raise ValueError("尝试状态非法")
    existing = db.scalar(  # type: ignore[attr-defined]
        select(ResearchTrialAttempt).where(
            ResearchTrialAttempt.experiment_id == experiment_id,
            ResearchTrialAttempt.trial_key == trial_key,
        )
    )
    if existing is not None:
        return existing
    now = datetime.now(UTC)
    payload = {
        "factor_spec": factor_spec,
        "parameters": parameters,
        "status": status,
        "metrics": metrics or {},
        "score_series": score_series or [],
        "error": error,
    }
    row = ResearchTrialAttempt(
        experiment_id=experiment_id,
        trial_key=trial_key,
        factor_spec=factor_spec,
        parameters=parameters,
        status=status,
        metrics=metrics or {},
        score_series=score_series or [],
        error=error,
        started_at=now,
        finished_at=now,
        result_sha256=_hash(payload),
    )
    db.add(row)  # type: ignore[attr-defined]
    db.flush()  # type: ignore[attr-defined]
    return row


def attempt_statistics(db: object, experiment_id: int) -> dict[str, object]:
    rows = db.scalars(  # type: ignore[attr-defined]
        select(ResearchTrialAttempt).where(
            ResearchTrialAttempt.experiment_id == experiment_id
        )
    ).all()
    series = [
        [float(value) for value in row.score_series]
        for row in rows
        if len(row.score_series) >= 3
    ]
    effective = float(len(rows))
    correlation: list[list[float]] = []
    if len(series) >= 2 and len({len(item) for item in series}) == 1:
        matrix = np.corrcoef(np.array(series))
        matrix = np.nan_to_num(matrix, nan=0.0)
        np.fill_diagonal(matrix, 1.0)
        eigenvalues = np.maximum(np.linalg.eigvalsh(matrix), 0.0)
        denominator = float(np.sum(eigenvalues**2))
        effective = (
            float(np.sum(eigenvalues) ** 2 / denominator)
            if denominator > 0
            else float(len(rows))
        )
        correlation = matrix.tolist()
    return {
        "total_attempts": len(rows),
        "completed": sum(row.status == "completed" for row in rows),
        "failed": sum(row.status == "failed" for row in rows),
        "abandoned": sum(row.status == "abandoned" for row in rows),
        "effective_attempts": effective,
        "attempt_correlation": correlation,
    }


def effective_attempt_count_from_series(
    score_series: list[list[float]],
) -> float:
    """相关尝试的参与率有效数量；供 DSR/PBO 使用。"""
    usable = [series for series in score_series if len(series) >= 3]
    if not usable:
        return 1.0
    if len(usable) == 1 or len({len(item) for item in usable}) != 1:
        return float(len(usable))
    matrix = np.corrcoef(np.array(usable))
    matrix = np.nan_to_num(matrix, nan=0.0)
    np.fill_diagonal(matrix, 1.0)
    eigenvalues = np.maximum(np.linalg.eigvalsh(matrix), 0.0)
    denominator = float(np.sum(eigenvalues**2))
    return (
        float(np.sum(eigenvalues) ** 2 / denominator)
        if denominator > 0
        else float(len(usable))
    )


def finalize_experiment(
    db: object,
    experiment_id: int,
    *,
    status: str,
    summary: dict[str, object],
) -> ResearchExperiment:
    if status not in {"completed", "failed", "abandoned"}:
        raise ValueError("实验终态非法")
    row = db.get(ResearchExperiment, experiment_id)  # type: ignore[attr-defined]
    if row is None or row.status != "running":
        raise ValueError("只有运行中实验可结束")
    row.status = status
    row.result_summary = {
        **summary,
        "attempt_statistics": attempt_statistics(db, experiment_id),
    }
    row.completed_at = datetime.now(UTC)
    db.flush()  # type: ignore[attr-defined]
    return row
