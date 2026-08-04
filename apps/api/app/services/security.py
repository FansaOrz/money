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

READONLY_POST_PREFIXES = (
    "/api/stocks/research/",
    "/api/quant/",
)


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
        if settings.environment.lower() != "production":
            response = await call_next(request)
            response.headers["X-Correlation-ID"] = correlation_id
            return response
        if settings.admin_api_key is None or settings.readonly_api_key is None:
            return JSONResponse(
                status_code=503,
                content={"detail": "生产认证密钥未配置", "correlation_id": correlation_id},
            )
        provided = request.headers.get("X-API-Key", "")
        if _matches(provided, settings.admin_api_key):
            role = "admin"
        elif _matches(provided, settings.readonly_api_key):
            role = "readonly"
        else:
            return JSONResponse(
                status_code=401,
                content={"detail": "API Key 无效", "correlation_id": correlation_id},
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
        response = await call_next(request)
        response.headers["X-Correlation-ID"] = correlation_id
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            db = SessionLocal()
            try:
                actor = f"{role}:{hashlib.sha256(provided.encode()).hexdigest()[:12]}"
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
