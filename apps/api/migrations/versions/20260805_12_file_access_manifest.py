"""add frozen file manifests and runtime access logs

Revision ID: 20260805_12
Revises: 20260805_11
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260805_12"
down_revision = "20260805_11"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    existing = set(inspector.get_table_names())
    if "data_file_manifest_entries" not in existing:
        op.create_table(
            "data_file_manifest_entries",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("snapshot_sha256", sa.String(64), nullable=False),
            sa.Column("relative_path", sa.String(700), nullable=False),
            sa.Column("size_bytes", sa.Integer(), nullable=False),
            sa.Column("file_sha256", sa.String(64), nullable=False),
            sa.Column("frozen_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint(
                "snapshot_sha256",
                "relative_path",
                name="uq_data_file_manifest_snapshot_path",
            ),
        )
        op.create_index(
            "ix_data_file_manifest_entries_snapshot_sha256",
            "data_file_manifest_entries",
            ["snapshot_sha256"],
        )
    if "data_file_access_logs" not in existing:
        op.create_table(
            "data_file_access_logs",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("snapshot_sha256", sa.String(64), nullable=False),
            sa.Column(
                "strategy_version_id",
                sa.Integer(),
                sa.ForeignKey(
                    "strategy_versions.id",
                    name="fk_file_access_strategy_version",
                ),
            ),
            sa.Column("relative_path", sa.String(700), nullable=False),
            sa.Column("observed_size_bytes", sa.Integer(), nullable=False),
            sa.Column("observed_sha256", sa.String(64), nullable=False),
            sa.Column("status", sa.String(30), nullable=False),
            sa.Column("detail", sa.Text(), nullable=False),
            sa.Column("accessed_at", sa.DateTime(timezone=True), nullable=False),
        )
        op.create_index(
            "ix_data_file_access_snapshot_version",
            "data_file_access_logs",
            ["snapshot_sha256", "strategy_version_id"],
        )


def downgrade() -> None:
    op.drop_index(
        "ix_data_file_access_snapshot_version",
        table_name="data_file_access_logs",
    )
    op.drop_table("data_file_access_logs")
    op.drop_index(
        "ix_data_file_manifest_entries_snapshot_sha256",
        table_name="data_file_manifest_entries",
    )
    op.drop_table("data_file_manifest_entries")
