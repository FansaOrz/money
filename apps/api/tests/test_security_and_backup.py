"""身份、传输门禁、审计链和加密异地备份。"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.config import get_settings
from app.db.session import get_db
from app.main import create_app
from app.models import AuditLog
from app.services import audit_chain, backup, identity
from app.config import Settings
from app.services.runtime_security import validate_runtime_exposure


def test_short_lived_identity_scope_mfa_and_individual_revocation(
    db_session, monkeypatch
) -> None:
    monkeypatch.setenv("MONEY_IDENTITY_SIGNING_KEY", "x" * 40)
    get_settings.cache_clear()
    identity.register_identity(
        db_session,
        identity_key="trader-a",
        identity_type="user",
        scopes=["read", "orders:write"],
        mfa_required=True,
    )
    with pytest.raises(PermissionError, match="MFA"):
        identity.issue_token(db_session, identity_key="trader-a")
    token = identity.issue_token(
        db_session, identity_key="trader-a", mfa_verified=True
    )
    claims = identity.verify_token(db_session, token)
    assert claims["sub"] == "trader-a"
    assert claims["mfa"] is True
    identity.revoke_identity(db_session, "trader-a", actor="security")
    with pytest.raises(PermissionError, match="吊销"):
        identity.verify_token(db_session, token)
    get_settings.cache_clear()


def test_audit_hash_chain_and_database_immutability(db_session) -> None:
    db_session.add(
        AuditLog(
            actor="test",
            action="validation.run",
            resource_type="strategy",
            resource_id="9",
            detail={"status": "failed"},
            created_at=datetime.now(UTC),
        )
    )
    db_session.commit()
    assert audit_chain.verify_audit_chain(db_session)["ok"] is True
    with pytest.raises(Exception):
        db_session.execute(text("UPDATE audit_logs SET actor='tampered'"))
        db_session.commit()
    db_session.rollback()


def test_audit_hash_chain_accepts_exact_legacy_migration_timestamp(
    db_session,
) -> None:
    created_at = "2026-08-04 16:15:21.774656"
    payload = {
        "previous_hash": "0" * 64,
        "actor": "legacy-migration",
        "action": "backfilled",
        "resource_type": "strategy",
        "resource_id": "1",
        "correlation_id": None,
        "detail": {"status": "legacy"},
        "created_at": created_at,
    }
    entry_hash = hashlib.sha256(
        json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode()
    ).hexdigest()
    db_session.execute(
        text(
            """
            INSERT INTO audit_logs (
                actor, action, resource_type, resource_id, correlation_id,
                detail, created_at, previous_hash, entry_hash
            ) VALUES (
                :actor, :action, :resource_type, :resource_id, NULL,
                :detail, :created_at, :previous_hash, :entry_hash
            )
            """
        ),
        {
            **payload,
            "detail": json.dumps(payload["detail"]),
            "entry_hash": entry_hash,
        },
    )
    db_session.commit()

    result = audit_chain.verify_audit_chain(db_session)
    assert result["ok"] is True
    assert result["legacy_encoding_count"] == 1


def test_encrypted_offsite_backup_restores_without_source_directory(
    db_session, tmp_path
) -> None:
    database = tmp_path / "source.db"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE sample(value INTEGER)")
        connection.execute("INSERT INTO sample VALUES (1)")
    research = tmp_path / "research"
    research.mkdir()
    (research / "bars.txt").write_text("point-in-time", encoding="utf-8")
    local = tmp_path / "local" / "v1"
    offsite = tmp_path / "offsite"
    key = "separate-secret-" * 3
    manifest = backup.create_backup(
        db_session,
        database_url=f"sqlite:///{database}",
        research_data_dir=research,
        destination=local,
        encryption_key=key,
        offsite_directory=offsite,
    )
    assert manifest["encrypted"] is True
    assert not list(local.glob("database.sqlite3"))
    replica = offsite / "v1"
    for path in sorted(local.glob("*")):
        path.unlink()
    local.rmdir()
    restored = backup.restore_to_new_directory(
        replica, tmp_path / "restored", encryption_key=key
    )
    assert (restored / "database.sqlite3").is_file()
    assert (restored / "research_data" / "bars.txt").read_text() == "point-in-time"


def test_pg_dump_command_never_contains_password(db_session, tmp_path, monkeypatch) -> None:
    captured: list[str] = []

    def fake_run(command, **kwargs):
        captured.extend(command)
        target = command[command.index("--file") + 1]
        __import__("pathlib").Path(target).write_bytes(b"dump")
        return type("Done", (), {"returncode": 0})()

    monkeypatch.setattr("app.services.backup.subprocess.run", fake_run)
    backup.create_backup(
        db_session,
        database_url="postgresql+psycopg://backup_user:very-secret@db:5432/money",
        research_data_dir=tmp_path / "research",
        destination=tmp_path / "backup",
    )
    assert "very-secret" not in " ".join(captured)
    assert "--no-password" in captured


def test_default_bind_is_loopback_and_public_development_fails_closed() -> None:
    settings = Settings()
    assert validate_runtime_exposure(
        settings, bind_host=settings.live_bind_host
    )["loopback_only"]
    with pytest.raises(RuntimeError, match="非 production"):
        validate_runtime_exposure(settings, bind_host="0.0.0.0")


def test_development_high_risk_key_is_independent(client, monkeypatch) -> None:
    monkeypatch.setenv("MONEY_HIGH_RISK_API_KEY", "separate-high-risk")
    get_settings.cache_clear()
    denied = client.post(
        "/api/quant-governance/accounts/SIM/kill-switch",
        params={"enabled": "true"},
    )
    assert denied.status_code == 401
    allowed = client.post(
        "/api/quant-governance/accounts/SIM/kill-switch",
        params={"enabled": "true"},
        headers={"X-High-Risk-Key": "separate-high-risk"},
    )
    assert allowed.status_code != 401
    monkeypatch.delenv("MONEY_HIGH_RISK_API_KEY")
    get_settings.cache_clear()


def test_production_rejects_remote_plaintext_before_auth(db_session, monkeypatch) -> None:
    monkeypatch.setenv("MONEY_ENVIRONMENT", "production")
    monkeypatch.setenv("MONEY_DATABASE_URL", "postgresql://u:p@db/money")
    monkeypatch.setenv("MONEY_AUTO_CREATE_TABLES", "false")
    monkeypatch.setenv("MONEY_ADMIN_API_KEY", "admin")
    monkeypatch.setenv("MONEY_READONLY_API_KEY", "readonly")
    get_settings.cache_clear()
    application = create_app()
    application.dependency_overrides[get_db] = lambda: iter([db_session])
    try:
        with TestClient(
            application, client=("198.51.100.2", 41234)
        ) as remote:
            response = remote.get(
                "/api/health", headers={"X-API-Key": "readonly"}
            )
            assert response.status_code == 400
            assert "明文" in response.json()["detail"]
    finally:
        application.dependency_overrides.clear()
        monkeypatch.delenv("MONEY_ENVIRONMENT")
        monkeypatch.delenv("MONEY_DATABASE_URL")
        monkeypatch.delenv("MONEY_AUTO_CREATE_TABLES")
        monkeypatch.delenv("MONEY_ADMIN_API_KEY")
        monkeypatch.delenv("MONEY_READONLY_API_KEY")
        get_settings.cache_clear()
