"""统一券商协议、不可变订单事件、乱序幂等归约和重放。"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import BrokerOrder, BrokerOrderEvent


EVENT_STATUS = {
    "submitted": "submitted",
    "acknowledged": "accepted",
    "rejected": "rejected",
    "partially_filled": "partially_filled",
    "filled": "filled",
    "cancel_requested": "cancel_pending",
    "cancelled": "cancelled",
    "cancel_rejected": "accepted",
    "expired": "expired",
    "fill_cancelled": "accepted",
    "fill_corrected": "filled",
}
TERMINAL = {"rejected", "filled", "cancelled", "expired"}


def reduce_events(events: list[BrokerOrderEvent]) -> str:
    """按券商序号（无则本地序号）确定性归约，终态不会被迟到旧事件倒退。"""
    ordered = sorted(
        events,
        key=lambda row: (
            row.broker_sequence
            if row.broker_sequence is not None
            else 10**12 + row.sequence,
            row.sequence,
        ),
    )
    status = "created"
    for event in ordered:
        next_status = EVENT_STATUS.get(event.event_type)
        if next_status is None:
            continue
        if status in TERMINAL and next_status not in TERMINAL:
            continue
        status = next_status
    return status


def append_event(
    db: Session,
    *,
    order_id: int,
    event_type: str,
    adapter: str,
    external_event_id: str,
    broker_sequence: int | None = None,
    broker_order_id: str | None = None,
    broker_batch_id: str | None = None,
    broker_fill_id: str | None = None,
    payload: dict[str, object] | None = None,
    occurred_at: datetime | None = None,
) -> BrokerOrderEvent:
    existing = db.scalar(
        select(BrokerOrderEvent).where(
            BrokerOrderEvent.adapter == adapter,
            BrokerOrderEvent.external_event_id == external_event_id,
        )
    )
    if existing is not None:
        return existing
    order = db.get(BrokerOrder, order_id)
    if order is None:
        raise ValueError("订单不存在")
    sequence = int(
        db.scalar(
            select(func.coalesce(func.max(BrokerOrderEvent.sequence), 0)).where(
                BrokerOrderEvent.order_id == order_id
            )
        )
        or 0
    ) + 1
    now = datetime.now(UTC)
    event = BrokerOrderEvent(
        order_id=order_id,
        sequence=sequence,
        broker_sequence=broker_sequence,
        event_type=event_type,
        adapter=adapter,
        external_event_id=external_event_id,
        broker_order_id=broker_order_id,
        broker_batch_id=broker_batch_id,
        broker_fill_id=broker_fill_id,
        payload=payload or {},
        occurred_at=occurred_at or now,
        received_at=now,
    )
    db.add(event)
    db.flush()
    events = list(
        db.scalars(
            select(BrokerOrderEvent).where(
                BrokerOrderEvent.order_id == order_id
            )
        ).all()
    )
    order.status = reduce_events(events)
    order.broker_order_id = broker_order_id or order.broker_order_id
    order.broker_batch_id = broker_batch_id or order.broker_batch_id
    order.updated_at = now
    return event


def replay_order(db: Session, order_id: int) -> dict[str, object]:
    events = list(
        db.scalars(
            select(BrokerOrderEvent)
            .where(BrokerOrderEvent.order_id == order_id)
            .order_by(BrokerOrderEvent.sequence)
        ).all()
    )
    return {
        "order_id": order_id,
        "status": reduce_events(events),
        "event_count": len(events),
        "events": [
            {
                "sequence": row.sequence,
                "broker_sequence": row.broker_sequence,
                "type": row.event_type,
                "external_event_id": row.external_event_id,
                "payload": row.payload,
            }
            for row in events
        ],
    }


class FakeBrokerAdapter:
    """测试异步乱序、重复、延迟和断线重连的确定性假券商。"""

    name = "fake_async"

    def __init__(self) -> None:
        self.connected = True
        self.buffer: list[dict[str, object]] = []

    def disconnect(self) -> None:
        self.connected = False

    def reconnect(self) -> list[dict[str, object]]:
        self.connected = True
        buffered = list(self.buffer)
        self.buffer.clear()
        return buffered

    def push(self, event: dict[str, object]) -> list[dict[str, object]]:
        if not self.connected:
            self.buffer.append(dict(event))
            return []
        return [dict(event)]
