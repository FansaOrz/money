"""add factor distribution health reports

Revision ID: 20260805_13
Revises: 20260805_12
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260805_13"
down_revision = "20260805_12"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if "factor_health_reports" in inspector.get_table_names():
        return
    op.create_table(
        "factor_health_reports",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "strategy_version_id",
            sa.Integer(),
            sa.ForeignKey("strategy_versions.id"),
        ),
        sa.Column("signal_date", sa.Date(), nullable=False),
        sa.Column("factor_name", sa.String(100), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("unit", sa.String(40), nullable=False),
        sa.Column("direction", sa.Integer(), nullable=False),
        sa.Column("statistics", sa.JSON(), nullable=False),
        sa.Column("reasons", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "strategy_version_id",
            "signal_date",
            "factor_name",
            name="uq_factor_health_version_date_factor",
        ),
    )
    op.create_index(
        "ix_factor_health_reports_strategy_version_id",
        "factor_health_reports",
        ["strategy_version_id"],
    )
    op.create_index(
        "ix_factor_health_status_date",
        "factor_health_reports",
        ["status", "signal_date"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_factor_health_status_date",
        table_name="factor_health_reports",
    )
    op.drop_index(
        "ix_factor_health_reports_strategy_version_id",
        table_name="factor_health_reports",
    )
    op.drop_table("factor_health_reports")
