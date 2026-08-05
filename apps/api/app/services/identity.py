"""短期签名身份、作用域授权、轮换与单主体吊销。"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import ApiIdentity, AuditLog


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode()


def _unb64(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _signing_key() -> bytes:
    configured = get_settings().identity_signing_key
    if configured is None or len(configured.get_secret_value()) < 32:
        raise RuntimeError("身份签名密钥未配置或少于 32 字符")
    return configured.get_secret_value().encode()


def register_identity(
    db: Session,
    *,
    identity_key: str,
    identity_type: str,
    scopes: list[str],
    mfa_required: bool = False,
    expires_at: datetime | None = None,
    actor: str = "security-admin",
) -> tuple[ApiIdentity, str]:
    if db.scalar(select(ApiIdentity).where(ApiIdentity.identity_key == identity_key)):
        raise ValueError("身份标识已存在")
    bootstrap_secret = secrets.token_urlsafe(32)
    row = ApiIdentity(
        identity_key=identity_key,
        identity_type=identity_type,
        secret_hash=hashlib.sha256(bootstrap_secret.encode()).hexdigest(),
        scopes=sorted(set(scopes)),
        active=True,
        mfa_required=mfa_required,
        rotated_at=datetime.now(UTC),
        expires_at=expires_at,
    )
    db.add(row)
    db.add(
        AuditLog(
            actor=actor,
            action="identity.create",
            resource_type="api_identity",
            resource_id=identity_key,
            detail={"scopes": row.scopes, "mfa_required": mfa_required},
            created_at=datetime.now(UTC),
        )
    )
    db.commit()
    db.refresh(row)
    return row, bootstrap_secret


def issue_token(
    db: Session,
    *,
    identity_key: str,
    ttl_seconds: int = 900,
    mfa_verified: bool = False,
) -> str:
    if not 30 <= ttl_seconds <= 3600:
        raise ValueError("短期凭据有效期必须在 30～3600 秒")
    identity = db.scalar(
        select(ApiIdentity).where(ApiIdentity.identity_key == identity_key)
    )
    now = datetime.now(UTC)
    if (
        identity is None
        or not identity.active
        or identity.expires_at is not None
        and identity.expires_at <= now
    ):
        raise PermissionError("身份不存在、已吊销或已过期")
    if identity.mfa_required and not mfa_verified:
        raise PermissionError("该身份签发凭据前必须完成 MFA")
    payload = {
        "sub": identity.identity_key,
        "typ": identity.identity_type,
        "scopes": identity.scopes,
        "mfa": mfa_verified,
        "iat": int(time.time()),
        "exp": int(time.time()) + ttl_seconds,
        "jti": secrets.token_hex(12),
    }
    encoded = _b64(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    )
    signature = _b64(hmac.new(_signing_key(), encoded.encode(), hashlib.sha256).digest())
    return f"{encoded}.{signature}"


def verify_token(db: Session, token: str) -> dict[str, object]:
    try:
        encoded, supplied = token.split(".", 1)
        expected = _b64(
            hmac.new(_signing_key(), encoded.encode(), hashlib.sha256).digest()
        )
        if not hmac.compare_digest(supplied, expected):
            raise PermissionError("凭据签名无效")
        payload = json.loads(_unb64(encoded))
    except (ValueError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise PermissionError("凭据格式无效") from exc
    if int(payload.get("exp", 0)) <= int(time.time()):
        raise PermissionError("凭据已过期")
    identity = db.scalar(
        select(ApiIdentity).where(ApiIdentity.identity_key == payload.get("sub"))
    )
    now = datetime.now(UTC)
    if (
        identity is None
        or not identity.active
        or identity.expires_at is not None
        and identity.expires_at <= now
    ):
        raise PermissionError("身份已吊销或过期")
    payload["scopes"] = sorted(set(payload.get("scopes", [])) & set(identity.scopes))
    return payload


def rotate_identity(db: Session, identity_key: str, *, actor: str) -> str:
    identity = db.scalar(
        select(ApiIdentity).where(ApiIdentity.identity_key == identity_key)
    )
    if identity is None:
        raise ValueError("身份不存在")
    secret = secrets.token_urlsafe(32)
    identity.secret_hash = hashlib.sha256(secret.encode()).hexdigest()
    identity.rotated_at = datetime.now(UTC)
    db.add(
        AuditLog(
            actor=actor,
            action="identity.rotate",
            resource_type="api_identity",
            resource_id=identity_key,
            detail={},
            created_at=datetime.now(UTC),
        )
    )
    db.commit()
    return secret


def revoke_identity(db: Session, identity_key: str, *, actor: str) -> None:
    identity = db.scalar(
        select(ApiIdentity).where(ApiIdentity.identity_key == identity_key)
    )
    if identity is None:
        raise ValueError("身份不存在")
    identity.active = False
    identity.expires_at = datetime.now(UTC)
    db.add(
        AuditLog(
            actor=actor,
            action="identity.revoke",
            resource_type="api_identity",
            resource_id=identity_key,
            detail={},
            created_at=datetime.now(UTC),
        )
    )
    db.commit()
