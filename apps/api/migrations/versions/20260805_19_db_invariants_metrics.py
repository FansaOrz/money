"""database invariants, optimistic locking and operational metrics

Revision ID: 20260805_19
Revises: 20260805_18
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260805_19"
down_revision = "20260805_18"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    ledger_columns = {
        item["name"] for item in inspector.get_columns("broker_account_ledgers")
    }
    with op.batch_alter_table("broker_account_ledgers") as batch:
        if "row_version" not in ledger_columns:
            batch.add_column(
                sa.Column(
                    "row_version",
                    sa.Integer(),
                    nullable=False,
                    server_default="1",
                )
            )
        batch.create_check_constraint(
            "ck_broker_ledger_cash_nonnegative", "cash >= 0"
        )
    with op.batch_alter_table("broker_orders") as batch:
        batch.create_check_constraint(
            "ck_broker_order_quantity_positive", "quantity > 0"
        )
        batch.create_check_constraint(
            "ck_broker_order_reference_price_positive",
            "reference_price IS NULL OR reference_price > 0",
        )
        batch.create_check_constraint(
            "ck_broker_order_side", "side IN ('buy', 'sell')"
        )
    with op.batch_alter_table("broker_fills") as batch:
        batch.create_check_constraint(
            "ck_broker_fill_quantity_nonzero", "quantity <> 0"
        )
        batch.create_check_constraint(
            "ck_broker_fill_price_positive", "price > 0"
        )
    if "operational_metrics" not in set(inspector.get_table_names()):
        op.create_table(
            "operational_metrics",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("metric_name", sa.String(100), nullable=False),
            sa.Column("value", sa.Numeric(24, 6), nullable=False),
            sa.Column("unit", sa.String(30), nullable=False),
            sa.Column("labels", sa.JSON(), nullable=False),
            sa.Column("budget", sa.JSON(), nullable=False),
            sa.Column("status", sa.String(20), nullable=False),
            sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        )
        op.create_index(
            "ix_operational_metric_name_time",
            "operational_metrics",
            ["metric_name", "observed_at"],
        )
    if bind.dialect.name == "sqlite":
        op.execute(
            """
            CREATE TRIGGER IF NOT EXISTS broker_fill_total_not_over_order
            BEFORE INSERT ON broker_fills
            WHEN NEW.event_type IN ('fill', 'correction')
            BEGIN
              SELECT CASE WHEN
                COALESCE((
                  SELECT SUM(quantity) FROM broker_fills
                  WHERE order_id = NEW.order_id
                ), 0) + NEW.quantity >
                (SELECT quantity FROM broker_orders WHERE id = NEW.order_id)
              THEN RAISE(ABORT, 'fill quantity exceeds order quantity') END;
            END
            """
        )
    elif bind.dialect.name == "postgresql":
        op.execute(
            """
            CREATE OR REPLACE FUNCTION enforce_fill_total() RETURNS trigger AS $$
            BEGIN
              IF NEW.event_type IN ('fill', 'correction') AND
                 COALESCE((SELECT SUM(quantity) FROM broker_fills
                           WHERE order_id = NEW.order_id), 0) + NEW.quantity >
                 (SELECT quantity FROM broker_orders WHERE id = NEW.order_id
                  FOR UPDATE) THEN
                RAISE EXCEPTION 'fill quantity exceeds order quantity';
              END IF;
              RETURN NEW;
            END;
            $$ LANGUAGE plpgsql;
            CREATE TRIGGER broker_fill_total_not_over_order
            BEFORE INSERT ON broker_fills
            FOR EACH ROW EXECUTE FUNCTION enforce_fill_total();
            """
        )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        op.execute("DROP TRIGGER IF EXISTS broker_fill_total_not_over_order")
    elif bind.dialect.name == "postgresql":
        op.execute("DROP TRIGGER IF EXISTS broker_fill_total_not_over_order ON broker_fills")
        op.execute("DROP FUNCTION IF EXISTS enforce_fill_total()")
    op.drop_index(
        "ix_operational_metric_name_time", table_name="operational_metrics"
    )
    op.drop_table("operational_metrics")
    with op.batch_alter_table("broker_fills") as batch:
        batch.drop_constraint("ck_broker_fill_price_positive", type_="check")
        batch.drop_constraint("ck_broker_fill_quantity_nonzero", type_="check")
    with op.batch_alter_table("broker_orders") as batch:
        batch.drop_constraint("ck_broker_order_side", type_="check")
        batch.drop_constraint(
            "ck_broker_order_reference_price_positive", type_="check"
        )
        batch.drop_constraint(
            "ck_broker_order_quantity_positive", type_="check"
        )
    with op.batch_alter_table("broker_account_ledgers") as batch:
        batch.drop_constraint(
            "ck_broker_ledger_cash_nonnegative", type_="check"
        )
        batch.drop_column("row_version")
