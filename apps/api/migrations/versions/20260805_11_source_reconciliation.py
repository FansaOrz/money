"""add cross-source reconciliation decisions

Revision ID: 20260805_11
Revises: 20260805_10
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260805_11"
down_revision = "20260805_10"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "data_source_reconciliations",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("dataset", sa.String(40), nullable=False),
        sa.Column("code", sa.String(20), nullable=False),
        sa.Column("effective_date", sa.Date(), nullable=False),
        sa.Column("field_name", sa.String(80), nullable=False),
        sa.Column("candidates", sa.JSON(), nullable=False),
        sa.Column("relative_difference", sa.Numeric(18, 8)),
        sa.Column("threshold", sa.Numeric(18, 8)),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("selected_source", sa.String(80)),
        sa.Column("selected_value", sa.Text()),
        sa.Column("rationale", sa.Text(), nullable=False),
        sa.Column("safe_action", sa.String(50), nullable=False),
        sa.Column("checked_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("resolved_by", sa.String(100)),
        sa.Column("resolved_at", sa.DateTime(timezone=True)),
    )
    op.create_index(
        "ix_source_reconciliation_status",
        "data_source_reconciliations",
        ["status", "dataset", "effective_date"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_source_reconciliation_status",
        table_name="data_source_reconciliations",
    )
    op.drop_table("data_source_reconciliations")
