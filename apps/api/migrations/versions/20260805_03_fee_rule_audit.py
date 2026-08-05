"""persist fee rule version and breakdown on every simulated fill

Revision ID: 20260805_03
Revises: 20260805_02
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260805_03"
down_revision = "20260805_02"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("stock_paper_trades") as batch:
        batch.add_column(
            sa.Column(
                "fee_rule_version",
                sa.String(length=100),
                nullable=False,
                server_default="LEGACY_UNDATED_COST_MODEL",
            )
        )
        batch.add_column(
            sa.Column(
                "fee_breakdown",
                sa.JSON(),
                nullable=False,
                server_default="{}",
            )
        )
    with op.batch_alter_table("broker_fills") as batch:
        batch.add_column(
            sa.Column(
                "fee_rule_version",
                sa.String(length=100),
                nullable=False,
                server_default="BROKER_REPORTED",
            )
        )
        batch.add_column(
            sa.Column(
                "fee_breakdown",
                sa.JSON(),
                nullable=False,
                server_default="{}",
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("broker_fills") as batch:
        batch.drop_column("fee_breakdown")
        batch.drop_column("fee_rule_version")
    with op.batch_alter_table("stock_paper_trades") as batch:
        batch.drop_column("fee_breakdown")
        batch.drop_column("fee_rule_version")

