"""identity, external alerts and tamper-evident audit chain

Revision ID: 20260805_20
Revises: 20260805_19
"""

from __future__ import annotations

import hashlib
import json

from alembic import op
import sqlalchemy as sa


revision = "20260805_20"
down_revision = "20260805_19"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    audit_columns = {item["name"] for item in inspector.get_columns("audit_logs")}
    with op.batch_alter_table("audit_logs") as batch:
        if "previous_hash" not in audit_columns:
            batch.add_column(sa.Column("previous_hash", sa.String(64)))
        if "entry_hash" not in audit_columns:
            batch.add_column(sa.Column("entry_hash", sa.String(64)))
    previous = "0" * 64
    rows = bind.execute(
        sa.text(
            "SELECT id, actor, action, resource_type, resource_id, "
            "correlation_id, detail, created_at FROM audit_logs ORDER BY id"
        )
    ).mappings()
    for row in rows:
        detail = row["detail"]
        if isinstance(detail, str):
            detail = json.loads(detail)
        created = row["created_at"]
        payload = {
            "previous_hash": previous,
            "actor": row["actor"],
            "action": row["action"],
            "resource_type": row["resource_type"],
            "resource_id": row["resource_id"],
            "correlation_id": row["correlation_id"],
            "detail": detail,
            "created_at": (
                created.isoformat() if hasattr(created, "isoformat") else str(created)
            ),
        }
        entry_hash = hashlib.sha256(
            json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        bind.execute(
            sa.text(
                "UPDATE audit_logs SET previous_hash=:previous, entry_hash=:entry "
                "WHERE id=:id"
            ),
            {"previous": previous, "entry": entry_hash, "id": row["id"]},
        )
        previous = entry_hash
    with op.batch_alter_table("audit_logs") as batch:
        batch.alter_column("previous_hash", nullable=False)
        batch.alter_column("entry_hash", nullable=False)
        batch.create_unique_constraint("uq_audit_entry_hash", ["entry_hash"])
    tables = set(inspector.get_table_names())
    if "external_alerts" not in tables:
        op.create_table(
            "external_alerts",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("dedup_key", sa.String(160), nullable=False),
            sa.Column("severity", sa.String(20), nullable=False),
            sa.Column("status", sa.String(20), nullable=False),
            sa.Column("payload", sa.JSON(), nullable=False),
            sa.Column("channel", sa.String(40), nullable=False),
            sa.Column("delivery_attempts", sa.Integer(), nullable=False),
            sa.Column("last_delivery_status", sa.String(30), nullable=False),
            sa.Column("acknowledged_by", sa.String(100)),
            sa.Column("escalation_level", sa.Integer(), nullable=False),
            sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("recovered_at", sa.DateTime(timezone=True)),
            sa.UniqueConstraint("dedup_key", "status", name="uq_alert_dedup_status"),
        )
    if "api_identities" not in tables:
        op.create_table(
            "api_identities",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("identity_key", sa.String(100), nullable=False, unique=True),
            sa.Column("identity_type", sa.String(20), nullable=False),
            sa.Column("secret_hash", sa.String(64), nullable=False),
            sa.Column("scopes", sa.JSON(), nullable=False),
            sa.Column("active", sa.Boolean(), nullable=False),
            sa.Column("mfa_required", sa.Boolean(), nullable=False),
            sa.Column("rotated_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("expires_at", sa.DateTime(timezone=True)),
        )
    if bind.dialect.name == "sqlite":
        op.execute(
            """
            CREATE TRIGGER IF NOT EXISTS prevent_audit_update
            BEFORE UPDATE ON audit_logs
            BEGIN SELECT RAISE(ABORT, 'audit log is immutable'); END
            """
        )
        op.execute(
            """
            CREATE TRIGGER IF NOT EXISTS prevent_audit_delete
            BEFORE DELETE ON audit_logs
            BEGIN SELECT RAISE(ABORT, 'audit log is immutable'); END
            """
        )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        op.execute("DROP TRIGGER IF EXISTS prevent_audit_delete")
        op.execute("DROP TRIGGER IF EXISTS prevent_audit_update")
    op.drop_table("api_identities")
    op.drop_table("external_alerts")
    with op.batch_alter_table("audit_logs") as batch:
        batch.drop_constraint("uq_audit_entry_hash", type_="unique")
        batch.drop_column("entry_hash")
        batch.drop_column("previous_hash")
