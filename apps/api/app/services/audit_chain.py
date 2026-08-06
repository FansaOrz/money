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
    legacy_encoding_count = 0
    for row in db.scalars(select(AuditLog).order_by(AuditLog.id)).all():
        created_at = (
            row.created_at.astimezone(UTC).replace(tzinfo=None)
            if row.created_at.tzinfo
            else row.created_at
        ).isoformat(timespec="microseconds")
        payload = {
            "previous_hash": previous,
            "actor": row.actor,
            "action": row.action,
            "resource_type": row.resource_type,
            "resource_id": row.resource_id,
            "correlation_id": row.correlation_id,
            "detail": row.detail,
            "created_at": created_at,
        }
        expected = _payload_hash(payload)
        matched_legacy = False
        if not hmac_compare(row.entry_hash, expected):
            # 20260805_20 在 SQLite 旧记录回填时直接哈希数据库时间字符串，
            # 使用空格而不是 ORM 新记录的 ISO ``T``。两者只允许这一处差异。
            legacy_payload = {
                **payload,
                "created_at": created_at.replace("T", " ", 1),
            }
            matched_legacy = hmac_compare(
                row.entry_hash, _payload_hash(legacy_payload)
            )
        if row.previous_hash != previous or (
            not hmac_compare(row.entry_hash, expected) and not matched_legacy
        ):
            return {"ok": False, "checked": checked, "broken_id": row.id}
        legacy_encoding_count += int(matched_legacy)
        previous = row.entry_hash
        checked += 1
    return {
        "ok": True,
        "checked": checked,
        "head": previous,
        "legacy_encoding_count": legacy_encoding_count,
    }


def _payload_hash(payload: dict[str, object]) -> str:
    return hashlib.sha256(
        json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode()
    ).hexdigest()


def hmac_compare(left: str, right: str) -> bool:
    return __import__("hmac").compare_digest(left, right)
