"""add correction impact and evidence hashes

Revision ID: 20260805_10
Revises: 20260805_09
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260805_10"
down_revision = "20260805_09"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("data_corrections") as batch:
        batch.add_column(
            sa.Column(
                "affected_strategy_versions",
                sa.JSON(),
                nullable=False,
                server_default="[]",
            )
        )
        batch.add_column(
            sa.Column(
                "evidence_sha256",
                sa.String(64),
                nullable=False,
                server_default="",
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("data_corrections") as batch:
        batch.drop_column("evidence_sha256")
        batch.drop_column("affected_strategy_versions")
