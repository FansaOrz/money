"""完全留出集查看、污染和版本重开规则。"""

from __future__ import annotations

from datetime import UTC, date, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import HoldoutConsumption


def interval_history(
    db: Session,
    interval_start: date,
    interval_end: date,
) -> list[HoldoutConsumption]:
    return list(
        db.scalars(
            select(HoldoutConsumption)
            .where(
                HoldoutConsumption.interval_start == interval_start,
                HoldoutConsumption.interval_end == interval_end,
            )
            .order_by(HoldoutConsumption.consumed_at, HoldoutConsumption.id)
        ).all()
    )


def assert_pristine(
    db: Session,
    interval_start: date,
    interval_end: date,
) -> None:
    history = interval_history(db, interval_start, interval_end)
    if history:
        experiments = ",".join(str(row.experiment_id) for row in history)
        raise ValueError(
            "完全留出区间已经永久消耗，不能再声明为 pristine；"
            f"历史实验={experiments}"
        )


def consume(
    db: Session,
    *,
    experiment_id: int,
    strategy_version_id: int | None,
    interval_start: date,
    interval_end: date,
    purpose: str,
    result_sha256: str,
    actor: str,
) -> HoldoutConsumption:
    if interval_end < interval_start:
        raise ValueError("留出区间结束日不能早于开始日")
    prior = interval_history(db, interval_start, interval_end)
    row = HoldoutConsumption(
        experiment_id=experiment_id,
        strategy_version_id=strategy_version_id,
        interval_start=interval_start,
        interval_end=interval_end,
        purpose=purpose,
        status="pristine_consumed" if not prior else "contaminated_reuse",
        result_sha256=result_sha256,
        consumed_by=actor,
        consumed_at=datetime.now(UTC),
    )
    db.add(row)
    db.flush()
    return row
