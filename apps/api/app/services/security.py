"""生产 API Key 认证、角色授权、关联 ID 与操作审计。"""

from __future__ import annotations

import hashlib
import hmac
import uuid
from datetime import UTC, datetime

from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

from app.config import get_settings
from app.db.session import SessionLocal
from app.models import AuditLog
from app.services.identity import verify_token

READONLY_POST_PREFIXES = (
    "/api/stocks/research/",
    "/api/quant/",
)

SENSITIVE_READ_FRAGMENTS = ("evidence", "holdout", "validation", "reconciliation")
HIGH_RISK_PATH_SCOPES = {
    "kill-switch": "kill_switch:execute",
    "/orders": "orders:write",
    "transition": "strategy:approve",
    "correction": "data:correct",
    "restore": "backup:restore",
    "/stocks/paper/run": "paper:run",
    "/stocks/paper/prepare": "paper:prepare",
    "data-quality/scan": "data:correct",
    "/reconcile": "reconciliation:write",
}


def _matches(provided: str, configured: object) -> bool:
    if configured is None:
        return False
    value = configured.get_secret_value()  # type: ignore[union-attr]
    return bool(value) and hmac.compare_digest(provided, value)


class ApiSecurityMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):  # type: ignore[no-untyped-def]
        settings = get_settings()
        correlation_id = request.headers.get("X-Correlation-ID") or uuid.uuid4().hex
        request.state.correlation_id = correlation_id
        client_host = request.client.host if request.client else ""
        forwarded_proto = request.headers.get("X-Forwarded-Proto", "")
        is_local = client_host in {"127.0.0.1", "::1", "localhost", "testclient"}
        if (
            settings.require_tls
            and not is_local
            and request.url.scheme != "https"
            and forwarded_proto != "https"
        ):
            return JSONResponse(
                status_code=400,
                content={"detail": "拒绝远程明文 HTTP", "correlation_id": correlation_id},
            )
        high_risk_marker = next(
            (marker for marker in HIGH_RISK_PATH_SCOPES if marker in request.url.path),
            None,
        )
        if settings.environment.lower() != "production":
            if (
                high_risk_marker is not None
                and request.method not in {"GET", "HEAD", "OPTIONS"}
                and settings.high_risk_api_key is not None
                and not _matches(
                    request.headers.get("X-High-Risk-Key", ""),
                    settings.high_risk_api_key,
                )
            ):
                return JSONResponse(
                    status_code=401,
                    content={
                        "detail": "开发态高风险操作需要独立凭据",
                        "correlation_id": correlation_id,
                    },
                )
            response = await call_next(request)
            response.headers["X-Correlation-ID"] = correlation_id
            return response
        if settings.admin_api_key is None or settings.readonly_api_key is None:
            return JSONResponse(
                status_code=503,
                content={"detail": "生产认证密钥未配置", "correlation_id": correlation_id},
            )
        provided = request.headers.get("X-API-Key", "")
        actor: str
        scopes: set[str]
        bearer = request.headers.get("Authorization", "")
        if bearer.startswith("Bearer "):
            db = SessionLocal()
            try:
                claims = verify_token(db, bearer.removeprefix("Bearer ").strip())
            except (PermissionError, RuntimeError) as exc:
                return JSONResponse(
                    status_code=401,
                    content={"detail": str(exc), "correlation_id": correlation_id},
                )
            finally:
                db.close()
            role = "identity"
            actor = f"identity:{claims['sub']}"
            scopes = set(claims.get("scopes", []))
            request.state.identity_claims = claims
        elif _matches(provided, settings.admin_api_key):
            role = "admin"
            actor = f"{role}:{hashlib.sha256(provided.encode()).hexdigest()[:12]}"
            scopes = {"*"}
        elif _matches(provided, settings.readonly_api_key):
            role = "readonly"
            actor = f"{role}:{hashlib.sha256(provided.encode()).hexdigest()[:12]}"
            scopes = {"read"}
        else:
            return JSONResponse(
                status_code=401,
                content={"detail": "身份凭据无效", "correlation_id": correlation_id},
            )
        readonly_allowed = request.method in {"GET", "HEAD", "OPTIONS"} or any(
            request.url.path.startswith(prefix)
            for prefix in READONLY_POST_PREFIXES
        )
        if role == "readonly" and not readonly_allowed:
            return JSONResponse(
                status_code=403,
                content={"detail": "只读角色无权修改", "correlation_id": correlation_id},
            )
        required_scope = next(
            (
                scope
                for marker, scope in HIGH_RISK_PATH_SCOPES.items()
                if marker in request.url.path
                and request.method not in {"GET", "HEAD", "OPTIONS"}
            ),
            None,
        )
        if role == "identity":
            generic_scope = "read" if readonly_allowed else "write"
            if (
                "*" not in scopes
                and generic_scope not in scopes
                and (required_scope is None or required_scope not in scopes)
            ):
                return JSONResponse(
                    status_code=403,
                    content={"detail": "身份缺少所需作用域", "correlation_id": correlation_id},
                )
        if required_scope is not None:
            claims = getattr(request.state, "identity_claims", {})
            second = request.headers.get("X-Second-Approver")
            if role != "identity" or not claims.get("mfa") or not second or second == claims.get("sub"):
                return JSONResponse(
                    status_code=403,
                    content={
                        "detail": "高风险操作要求 MFA 短期身份及独立第二审批人",
                        "correlation_id": correlation_id,
                    },
                )
            if (
                high_risk_marker != "kill-switch"
                and (
                    settings.environment.lower() == "production"
                    or settings.enforce_high_risk_preflight
                )
            ):
                from app.services.platform_preflight import (
                    evaluate_system_preflight,
                )

                idempotency_key = request.headers.get("Idempotency-Key", "")
                confirmation = request.headers.get("X-Confirmation-Digest")
                preflight_db = SessionLocal()
                try:
                    preflight = evaluate_system_preflight(
                        preflight_db,
                        operation=f"{request.method} {request.url.path}",
                        target=request.url.path,
                        impact="高风险写操作",
                        idempotency_key=idempotency_key,
                        confirmation_digest=confirmation,
                    )
                finally:
                    preflight_db.close()
                if not preflight["allowed"]:
                    return JSONResponse(
                        status_code=409,
                        content={
                            "detail": "高风险预检查未通过",
                            "preflight": preflight,
                            "correlation_id": correlation_id,
                        },
                    )
        response = await call_next(request)
        response.headers["X-Correlation-ID"] = correlation_id
        audit_read = any(
            fragment in request.url.path for fragment in SENSITIVE_READ_FRAGMENTS
        )
        if request.method not in {"GET", "HEAD", "OPTIONS"} or audit_read:
            db = SessionLocal()
            try:
                db.add(
                    AuditLog(
                        actor=actor,
                        action=f"{request.method} {request.url.path}",
                        resource_type="api",
                        resource_id=None,
                        correlation_id=correlation_id,
                        detail={"status_code": response.status_code},
                        created_at=datetime.now(UTC),
                    )
                )
                db.commit()
            finally:
                db.close()
        return response
