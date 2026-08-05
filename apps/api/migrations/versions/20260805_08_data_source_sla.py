"""add required dataset source policies and SLA states

Revision ID: 20260805_08
Revises: 20260805_07
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260805_08"
down_revision = "20260805_07"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "data_source_sla_states",
        sa.Column("dataset", sa.String(40), primary_key=True),
        sa.Column(
            "required", sa.Boolean(), nullable=False, server_default=sa.true()
        ),
        sa.Column("primary_source", sa.String(80), nullable=False),
        sa.Column("fallback_source", sa.String(80)),
        sa.Column("license_class", sa.String(30), nullable=False),
        sa.Column("frequency_minutes", sa.Integer(), nullable=False),
        sa.Column("max_latency_minutes", sa.Integer(), nullable=False),
        sa.Column("rate_limit", sa.String(100)),
        sa.Column("owner", sa.String(100), nullable=False),
        sa.Column("failure_mode", sa.String(50), nullable=False),
        sa.Column(
            "status",
            sa.String(20),
            nullable=False,
            server_default="never_run",
        ),
        sa.Column("active_source", sa.String(80)),
        sa.Column("last_attempted_at", sa.DateTime(timezone=True)),
        sa.Column("last_success_at", sa.DateTime(timezone=True)),
        sa.Column("data_date", sa.Date()),
        sa.Column("schema_hash", sa.String(64)),
        sa.Column("row_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "consecutive_failures",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "degraded", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
        sa.Column(
            "escalation_level",
            sa.String(20),
            nullable=False,
            server_default="none",
        ),
        sa.Column("error", sa.Text()),
        sa.Column("detail", sa.JSON(), nullable=False, server_default="{}"),
    )


def downgrade() -> None:
    op.drop_table("data_source_sla_states")
