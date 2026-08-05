"""独立 Webhook 外部告警、去重、恢复通知和未确认升级。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Callable

import requests
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import ExternalAlert


def _deliver(payload: dict[str, object]) -> None:
    setting = get_settings().alert_webhook_url
    if setting is None:
        raise RuntimeError("未配置独立外部告警 Webhook")
    requests.post(
        setting.get_secret_value(),
        json=payload,
        timeout=10,
    ).raise_for_status()


def emit_alert(
    db: Session,
    *,
    dedup_key: str,
    severity: str,
    strategy: str | None,
    account: str | None,
    impact: str,
    correlation_id: str,
    action_url: str,
    sender: Callable[[dict[str, object]], None] = _deliver,
) -> ExternalAlert:
    existing = db.scalar(
        select(ExternalAlert).where(
            ExternalAlert.dedup_key == dedup_key,
            ExternalAlert.status == "open",
        )
    )
    now = datetime.now(UTC)
    if existing is not None:
        existing.last_seen_at = now
        db.commit()
        return existing
    payload = {
        "event": "alert",
        "severity": severity,
        "strategy": strategy,
        "account": account,
        "time": now.isoformat(),
        "impact": impact,
        "correlation_id": correlation_id,
        "action_url": action_url,
    }
    delivery_status = "sent"
    attempts = 1
    try:
        sender(payload)
    except Exception as exc:  # noqa: BLE001
        delivery_status = f"failed:{type(exc).__name__}"
    row = ExternalAlert(
        dedup_key=dedup_key,
        severity=severity,
        status="open",
        payload=payload,
        channel="webhook",
        delivery_attempts=attempts,
        last_delivery_status=delivery_status,
        escalation_level=0,
        first_seen_at=now,
        last_seen_at=now,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def escalate_unacknowledged(
    db: Session,
    *,
    sender: Callable[[dict[str, object]], None] = _deliver,
) -> int:
    cutoff = datetime.now(UTC) - timedelta(
        minutes=get_settings().alert_ack_sla_minutes
    )
    rows = list(
        db.scalars(
            select(ExternalAlert).where(
                ExternalAlert.status == "open",
                ExternalAlert.acknowledged_by.is_(None),
                ExternalAlert.first_seen_at <= cutoff,
            )
        ).all()
    )
    for row in rows:
        row.escalation_level += 1
        payload = {
            **dict(row.payload),
            "event": "escalation",
            "escalation_level": row.escalation_level,
        }
        try:
            sender(payload)
            row.last_delivery_status = "escalated"
        except Exception as exc:  # noqa: BLE001
            row.last_delivery_status = f"failed:{type(exc).__name__}"
        row.delivery_attempts += 1
    db.commit()
    return len(rows)


def recover_alert(
    db: Session,
    *,
    dedup_key: str,
    sender: Callable[[dict[str, object]], None] = _deliver,
) -> ExternalAlert:
    row = db.scalar(
        select(ExternalAlert).where(
            ExternalAlert.dedup_key == dedup_key,
            ExternalAlert.status == "open",
        )
    )
    if row is None:
        raise ValueError("未找到开放告警")
    row.status = "recovered"
    row.recovered_at = datetime.now(UTC)
    payload = {
        **dict(row.payload),
        "event": "recovery",
        "recovered_at": row.recovered_at.isoformat(),
    }
    sender(payload)
    row.delivery_attempts += 1
    row.last_delivery_status = "recovery_sent"
    db.commit()
    return row
