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
    """返回与给定区间存在任意日期重叠的永久查看记录。"""
    return list(
        db.scalars(
            select(HoldoutConsumption)
            .where(
                HoldoutConsumption.interval_start <= interval_end,
                HoldoutConsumption.interval_end >= interval_start,
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
        overlaps = "、".join(
            (
                f"实验{row.experiment_id}:"
                f"{row.interval_start.isoformat()}~{row.interval_end.isoformat()}"
            )
            for row in history
        )
        raise ValueError(
            "完全留出区间与已经永久消耗的区间重叠，"
            f"不能再声明为 pristine；重叠记录={overlaps}"
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
