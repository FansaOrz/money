"""启动绑定、环境、认证和传输配置的 fail-closed 检查。"""

from __future__ import annotations

import ipaddress

from app.config import Settings


def validate_runtime_exposure(settings: Settings, *, bind_host: str) -> dict[str, object]:
    try:
        address = ipaddress.ip_address(bind_host)
        loopback = address.is_loopback
    except ValueError:
        loopback = bind_host.lower() == "localhost"
    public_bind = bind_host in {"0.0.0.0", "::"} or not loopback
    blockers: list[str] = []
    if public_bind and settings.environment.lower() != "production":
        blockers.append("非 production 环境禁止对外绑定")
    if public_bind and (
        settings.admin_api_key is None or settings.readonly_api_key is None
    ):
        blockers.append("对外绑定缺少认证密钥")
    if public_bind and not settings.require_tls:
        blockers.append("对外绑定必须强制 TLS")
    if blockers:
        raise RuntimeError("；".join(blockers))
    return {
        "bind_host": bind_host,
        "loopback_only": not public_bind,
        "environment": settings.environment,
        "authentication_configured": (
            settings.admin_api_key is not None
            and settings.readonly_api_key is not None
        ),
        "tls_required": settings.require_tls,
    }
