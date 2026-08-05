"""不可篡改审计哈希链的独立复算校验。"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import AuditLog


def verify_audit_chain(db: Session) -> dict[str, object]:
    previous = "0" * 64
    checked = 0
    for row in db.scalars(select(AuditLog).order_by(AuditLog.id)).all():
        payload = {
            "previous_hash": previous,
            "actor": row.actor,
            "action": row.action,
            "resource_type": row.resource_type,
            "resource_id": row.resource_id,
            "correlation_id": row.correlation_id,
            "detail": row.detail,
            "created_at": (
                row.created_at.astimezone(UTC).replace(tzinfo=None)
                if row.created_at.tzinfo
                else row.created_at
            ).isoformat(timespec="microseconds"),
        }
        expected = hashlib.sha256(
            json.dumps(
                payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ).encode()
        ).hexdigest()
        if row.previous_hash != previous or not hmac_compare(row.entry_hash, expected):
            return {"ok": False, "checked": checked, "broken_id": row.id}
        previous = row.entry_hash
        checked += 1
    return {"ok": True, "checked": checked, "head": previous}


def hmac_compare(left: str, right: str) -> bool:
    return __import__("hmac").compare_digest(left, right)
