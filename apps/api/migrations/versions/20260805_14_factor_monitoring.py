"""add factor decay and crowding monitoring snapshots

Revision ID: 20260805_14
Revises: 20260805_13
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260805_14"
down_revision = "20260805_13"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if "factor_monitor_snapshots" in inspector.get_table_names():
        return
    op.create_table(
        "factor_monitor_snapshots",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "strategy_version_id",
            sa.Integer(),
            sa.ForeignKey("strategy_versions.id"),
        ),
        sa.Column("as_of", sa.Date(), nullable=False),
        sa.Column("factor_name", sa.String(100), nullable=False),
        sa.Column("action", sa.String(30), nullable=False),
        sa.Column("metrics", sa.JSON(), nullable=False),
        sa.Column("reasons", sa.JSON(), nullable=False),
        sa.Column("policy_version", sa.String(50), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "strategy_version_id",
            "as_of",
            "factor_name",
            name="uq_factor_monitor_version_date_factor",
        ),
    )
    op.create_index(
        "ix_factor_monitor_snapshots_strategy_version_id",
        "factor_monitor_snapshots",
        ["strategy_version_id"],
    )
    op.create_index(
        "ix_factor_monitor_action_date",
        "factor_monitor_snapshots",
        ["action", "as_of"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_factor_monitor_action_date",
        table_name="factor_monitor_snapshots",
    )
    op.drop_index(
        "ix_factor_monitor_snapshots_strategy_version_id",
        table_name="factor_monitor_snapshots",
    )
    op.drop_table("factor_monitor_snapshots")
