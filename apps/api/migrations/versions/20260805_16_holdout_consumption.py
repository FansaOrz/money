"""add permanent holdout consumption registry

Revision ID: 20260805_16
Revises: 20260805_15
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260805_16"
down_revision = "20260805_15"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if "holdout_consumptions" in set(sa.inspect(op.get_bind()).get_table_names()):
        return
    op.create_table(
        "holdout_consumptions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "experiment_id",
            sa.Integer(),
            sa.ForeignKey("research_experiments.id"),
            nullable=False,
        ),
        sa.Column(
            "strategy_version_id",
            sa.Integer(),
            sa.ForeignKey("strategy_versions.id"),
        ),
        sa.Column("interval_start", sa.Date(), nullable=False),
        sa.Column("interval_end", sa.Date(), nullable=False),
        sa.Column("purpose", sa.String(100), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("result_sha256", sa.String(64), nullable=False),
        sa.Column("consumed_by", sa.String(100), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "experiment_id",
            "interval_start",
            "interval_end",
            name="uq_holdout_consumption_experiment_interval",
        ),
    )
    op.create_index(
        "ix_holdout_consumptions_experiment_id",
        "holdout_consumptions",
        ["experiment_id"],
    )
    op.create_index(
        "ix_holdout_consumptions_strategy_version_id",
        "holdout_consumptions",
        ["strategy_version_id"],
    )
    op.create_index(
        "ix_holdout_consumption_interval",
        "holdout_consumptions",
        ["interval_start", "interval_end"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_holdout_consumption_interval",
        table_name="holdout_consumptions",
    )
    op.drop_index(
        "ix_holdout_consumptions_strategy_version_id",
        table_name="holdout_consumptions",
    )
    op.drop_index(
        "ix_holdout_consumptions_experiment_id",
        table_name="holdout_consumptions",
    )
    op.drop_table("holdout_consumptions")
