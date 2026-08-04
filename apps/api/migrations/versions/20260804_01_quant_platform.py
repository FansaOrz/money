"""量化平台治理表。

Revision ID: 20260804_01
Revises:
"""

from alembic import op
import sqlalchemy as sa

revision = "20260804_01"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 既有表按单表 checkfirst 建立基线，新环境也能从空库升级；整个过程
    # 不调用 metadata.create_all。
    from app.db.base import Base
    from app import models  # noqa: F401

    bind = op.get_bind()
    for table in Base.metadata.sorted_tables:
        if table.name != "persistent_jobs":
            table.create(bind, checkfirst=True)
    inspector = sa.inspect(bind)
    if "persistent_jobs" not in set(inspector.get_table_names()):
        op.create_table(
            "persistent_jobs",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("job_name", sa.String(80), nullable=False),
            sa.Column(
                "scheduled_for", sa.DateTime(timezone=True), nullable=False
            ),
            sa.Column("status", sa.String(20), nullable=False),
            sa.Column("attempt", sa.Integer(), nullable=False),
            sa.Column("max_attempts", sa.Integer(), nullable=False),
            sa.Column("locked_by", sa.String(100)),
            sa.Column("locked_until", sa.DateTime(timezone=True)),
            sa.Column("depends_on", sa.JSON(), nullable=False),
            sa.Column("payload", sa.JSON(), nullable=False),
            sa.Column("checkpoint", sa.JSON(), nullable=False),
            sa.Column("correlation_id", sa.String(64), nullable=False),
            sa.Column("error", sa.Text()),
            sa.Column("finished_at", sa.DateTime(timezone=True)),
            sa.UniqueConstraint(
                "job_name", "scheduled_for", name="uq_job_schedule"
            ),
        )
        op.create_index(
            "ix_persistent_job_claim",
            "persistent_jobs",
            ["status", "scheduled_for", "locked_until"],
        )
    elif "ix_persistent_job_claim" not in {
        item["name"] for item in inspector.get_indexes("persistent_jobs")
    }:
        op.create_index(
            "ix_persistent_job_claim",
            "persistent_jobs",
            ["status", "scheduled_for", "locked_until"],
        )
    # 每张治理表由本 revision 显式创建；不调用 Base.metadata.create_all。


def downgrade() -> None:
    for table in (
        "risk_control_state",
        "broker_account_ledgers",
        "broker_fills",
        "broker_orders",
        "strategy_transitions",
        "audit_logs",
        "data_readiness_reports",
        "quant_import_runs",
        "data_corrections",
        "data_quality_issues",
        "data_field_provenance",
        "quant_data_records",
    ):
        op.drop_table(table)
    op.drop_index("ix_persistent_job_claim", table_name="persistent_jobs")
    op.drop_table("persistent_jobs")
