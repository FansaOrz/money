"""event sourced OMS and persistent reconciliation controls

Revision ID: 20260805_18
Revises: 20260805_17
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260805_18"
down_revision = "20260805_17"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())
    order_columns = {item["name"] for item in inspector.get_columns("broker_orders")}
    with op.batch_alter_table("broker_orders") as batch:
        if "broker_order_id" not in order_columns:
            batch.add_column(sa.Column("broker_order_id", sa.String(100)))
            batch.create_index("ix_broker_orders_broker_order_id", ["broker_order_id"])
        if "broker_batch_id" not in order_columns:
            batch.add_column(sa.Column("broker_batch_id", sa.String(100)))
    fill_columns = {item["name"] for item in inspector.get_columns("broker_fills")}
    with op.batch_alter_table("broker_fills") as batch:
        if "account" not in fill_columns:
            batch.add_column(
                sa.Column(
                    "account",
                    sa.String(100),
                    nullable=False,
                    server_default="legacy",
                )
            )
        if "trade_date" not in fill_columns:
            batch.add_column(sa.Column("trade_date", sa.Date(), nullable=True))
        if "event_type" not in fill_columns:
            batch.add_column(
                sa.Column(
                    "event_type",
                    sa.String(20),
                    nullable=False,
                    server_default="fill",
                )
            )
        if "original_external_fill_id" not in fill_columns:
            batch.add_column(
                sa.Column("original_external_fill_id", sa.String(100))
            )
    op.execute(
        "UPDATE broker_fills SET trade_date = date(filled_at) "
        "WHERE trade_date IS NULL"
    )
    with op.batch_alter_table("broker_fills") as batch:
        batch.alter_column("trade_date", nullable=False)
        batch.drop_constraint("uq_broker_fill_external", type_="unique")
        batch.create_unique_constraint(
            "uq_broker_fill_external",
            ["adapter", "account", "trade_date", "external_fill_id"],
        )
    if "broker_order_events" not in tables:
        op.create_table(
            "broker_order_events",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("order_id", sa.Integer(), sa.ForeignKey("broker_orders.id"), nullable=False),
            sa.Column("sequence", sa.Integer(), nullable=False),
            sa.Column("broker_sequence", sa.Integer()),
            sa.Column("event_type", sa.String(30), nullable=False),
            sa.Column("adapter", sa.String(40), nullable=False),
            sa.Column("external_event_id", sa.String(120), nullable=False),
            sa.Column("broker_order_id", sa.String(100)),
            sa.Column("broker_batch_id", sa.String(100)),
            sa.Column("broker_fill_id", sa.String(100)),
            sa.Column("payload", sa.JSON(), nullable=False),
            sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint("order_id", "sequence", name="uq_order_event_sequence"),
            sa.UniqueConstraint("adapter", "external_event_id", name="uq_order_event_external"),
        )
        op.create_index("ix_broker_order_events_order_id", "broker_order_events", ["order_id"])
    if "reconciliation_runs" not in tables:
        op.create_table(
            "reconciliation_runs",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("account", sa.String(100), nullable=False),
            sa.Column("adapter", sa.String(40), nullable=False),
            sa.Column("trade_date", sa.Date(), nullable=False),
            sa.Column("status", sa.String(20), nullable=False),
            sa.Column("broker_snapshot_sha256", sa.String(64), nullable=False),
            sa.Column("broker_snapshot", sa.JSON(), nullable=False),
            sa.Column("local_snapshot", sa.JSON(), nullable=False),
            sa.Column("tolerance", sa.JSON(), nullable=False),
            sa.Column("categories", sa.JSON(), nullable=False),
            sa.Column("responsible_owner", sa.String(100)),
            sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("completed_at", sa.DateTime(timezone=True), nullable=False),
        )
        op.create_index("ix_reconciliation_runs_account", "reconciliation_runs", ["account"])
        op.create_index("ix_reconciliation_runs_trade_date", "reconciliation_runs", ["trade_date"])
    if "reconciliation_breaks" not in tables:
        op.create_table(
            "reconciliation_breaks",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("run_id", sa.Integer(), sa.ForeignKey("reconciliation_runs.id"), nullable=False),
            sa.Column("break_type", sa.String(40), nullable=False),
            sa.Column("code", sa.String(20)),
            sa.Column("expected", sa.JSON(), nullable=False),
            sa.Column("actual", sa.JSON(), nullable=False),
            sa.Column("difference", sa.JSON(), nullable=False),
            sa.Column("status", sa.String(20), nullable=False),
            sa.Column("owner", sa.String(100)),
            sa.Column("resolution", sa.JSON(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("resolved_at", sa.DateTime(timezone=True)),
        )
        op.create_index("ix_reconciliation_breaks_run_id", "reconciliation_breaks", ["run_id"])
    if "strategy_control_states" not in tables:
        op.create_table(
            "strategy_control_states",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("account", sa.String(100), nullable=False),
            sa.Column("strategy_version_id", sa.Integer(), sa.ForeignKey("strategy_versions.id")),
            sa.Column("mode", sa.String(20), nullable=False),
            sa.Column("reason", sa.Text(), nullable=False),
            sa.Column("operator", sa.String(100), nullable=False),
            sa.Column("approver", sa.String(100)),
            sa.Column("scope", sa.JSON(), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint("account", "strategy_version_id", name="uq_strategy_control_account_version"),
        )
        op.create_index("ix_strategy_control_states_account", "strategy_control_states", ["account"])
    if "kill_switch_drills" not in tables:
        op.create_table(
            "kill_switch_drills",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("account", sa.String(100), nullable=False),
            sa.Column("triggered_by", sa.String(100), nullable=False),
            sa.Column("approved_by", sa.String(100), nullable=False),
            sa.Column("policy", sa.JSON(), nullable=False),
            sa.Column("cancelled_orders", sa.JSON(), nullable=False),
            sa.Column("failed_orders", sa.JSON(), nullable=False),
            sa.Column("elapsed_ms", sa.Integer(), nullable=False),
            sa.Column("sla_ms", sa.Integer(), nullable=False),
            sa.Column("passed", sa.Boolean(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        )
        op.create_index("ix_kill_switch_drills_account", "kill_switch_drills", ["account"])


def downgrade() -> None:
    for table in (
        "kill_switch_drills",
        "strategy_control_states",
        "reconciliation_breaks",
        "reconciliation_runs",
        "broker_order_events",
    ):
        op.drop_table(table)
    with op.batch_alter_table("broker_fills") as batch:
        batch.drop_constraint("uq_broker_fill_external", type_="unique")
        batch.create_unique_constraint(
            "uq_broker_fill_external", ["adapter", "external_fill_id"]
        )
        batch.drop_column("original_external_fill_id")
        batch.drop_column("event_type")
        batch.drop_column("trade_date")
        batch.drop_column("account")
    with op.batch_alter_table("broker_orders") as batch:
        batch.drop_column("broker_batch_id")
        batch.drop_column("broker_order_id")
