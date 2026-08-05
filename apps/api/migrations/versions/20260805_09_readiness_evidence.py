"""bind readiness reports to strategy versions and data snapshots

Revision ID: 20260805_09
Revises: 20260805_08
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260805_09"
down_revision = "20260805_08"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("data_readiness_reports") as batch:
        batch.drop_constraint(
            "uq_data_readiness_strategy_day_code", type_="unique"
        )
        batch.add_column(
            sa.Column(
                "strategy_version_id",
                sa.Integer(),
                sa.ForeignKey(
                    "strategy_versions.id",
                    name="fk_data_readiness_strategy_version",
                ),
            )
        )
        batch.add_column(
            sa.Column(
                "data_snapshot_sha256",
                sa.String(64),
                nullable=False,
                server_default="",
            )
        )
        batch.add_column(
            sa.Column(
                "report_sha256",
                sa.String(64),
                nullable=False,
                server_default="",
            )
        )
        batch.create_unique_constraint(
            "uq_data_readiness_strategy_version_day_code",
            [
                "strategy_name",
                "strategy_version_id",
                "signal_date",
                "code",
            ],
        )
        batch.create_index(
            "ix_data_readiness_reports_strategy_version_id",
            ["strategy_version_id"],
        )


def downgrade() -> None:
    with op.batch_alter_table("data_readiness_reports") as batch:
        batch.drop_index(
            "ix_data_readiness_reports_strategy_version_id"
        )
        batch.drop_constraint(
            "uq_data_readiness_strategy_version_day_code",
            type_="unique",
        )
        batch.drop_column("report_sha256")
        batch.drop_column("data_snapshot_sha256")
        batch.drop_column("strategy_version_id")
        batch.create_unique_constraint(
            "uq_data_readiness_strategy_day_code",
            ["strategy_name", "signal_date", "code"],
        )
