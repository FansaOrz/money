"""add immutable research experiment and trial registry

Revision ID: 20260805_15
Revises: 20260805_14
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260805_15"
down_revision = "20260805_14"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    existing = set(inspector.get_table_names())
    if "research_experiments" not in existing:
        op.create_table(
            "research_experiments",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("experiment_key", sa.String(100), nullable=False, unique=True),
            sa.Column("hypothesis", sa.Text(), nullable=False),
            sa.Column("parameter_space", sa.JSON(), nullable=False),
            sa.Column("target_metrics", sa.JSON(), nullable=False),
            sa.Column("data_scope", sa.JSON(), nullable=False),
            sa.Column("status", sa.String(20), nullable=False),
            sa.Column("registered_by", sa.String(100), nullable=False),
            sa.Column("registered_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("started_at", sa.DateTime(timezone=True)),
            sa.Column("completed_at", sa.DateTime(timezone=True)),
            sa.Column("result_summary", sa.JSON(), nullable=False),
            sa.Column("registration_sha256", sa.String(64), nullable=False),
        )
    if "research_trial_attempts" not in existing:
        op.create_table(
            "research_trial_attempts",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column(
                "experiment_id",
                sa.Integer(),
                sa.ForeignKey("research_experiments.id"),
                nullable=False,
            ),
            sa.Column("trial_key", sa.String(120), nullable=False),
            sa.Column("factor_spec", sa.JSON(), nullable=False),
            sa.Column("parameters", sa.JSON(), nullable=False),
            sa.Column("status", sa.String(20), nullable=False),
            sa.Column("metrics", sa.JSON(), nullable=False),
            sa.Column("score_series", sa.JSON(), nullable=False),
            sa.Column("error", sa.Text()),
            sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("finished_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("result_sha256", sa.String(64), nullable=False),
            sa.UniqueConstraint(
                "experiment_id",
                "trial_key",
                name="uq_research_trial_experiment_key",
            ),
        )
        op.create_index(
            "ix_research_trial_attempts_experiment_id",
            "research_trial_attempts",
            ["experiment_id"],
        )
    dialect = op.get_bind().dialect.name
    if dialect == "sqlite":
        op.execute(
            """
            CREATE TRIGGER IF NOT EXISTS prevent_research_experiment_delete
            BEFORE DELETE ON research_experiments
            BEGIN
              SELECT RAISE(ABORT, 'research experiment history is immutable');
            END
            """
        )
        op.execute(
            """
            CREATE TRIGGER IF NOT EXISTS prevent_research_trial_delete
            BEFORE DELETE ON research_trial_attempts
            BEGIN
              SELECT RAISE(ABORT, 'research trial history is immutable');
            END
            """
        )


def downgrade() -> None:
    if op.get_bind().dialect.name == "sqlite":
        op.execute("DROP TRIGGER IF EXISTS prevent_research_trial_delete")
        op.execute("DROP TRIGGER IF EXISTS prevent_research_experiment_delete")
    op.drop_index(
        "ix_research_trial_attempts_experiment_id",
        table_name="research_trial_attempts",
    )
    op.drop_table("research_trial_attempts")
    op.drop_table("research_experiments")
