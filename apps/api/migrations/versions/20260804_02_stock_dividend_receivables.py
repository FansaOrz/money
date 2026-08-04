"""股票前向模拟现金股利应收账本。

Revision ID: 20260804_02
Revises: 20260804_01
"""

from alembic import op
import sqlalchemy as sa

revision = "20260804_02"
down_revision = "20260804_01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    signal_columns = {
        column["name"]
        for column in inspector.get_columns("stock_paper_signals")
    }
    if "order_state" not in signal_columns:
        with op.batch_alter_table("stock_paper_signals") as batch:
            batch.add_column(
                sa.Column(
                    "order_state",
                    sa.JSON(),
                    nullable=False,
                    server_default=sa.text("'{}'"),
                )
            )
    if "stock_paper_receivables" in set(inspector.get_table_names()):
        return
    op.create_table(
        "stock_paper_receivables",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("account_id", sa.Integer(), nullable=False),
        sa.Column("stock_code", sa.String(10), nullable=False),
        sa.Column("event_key", sa.String(160), nullable=False),
        sa.Column("entitlement_date", sa.Date(), nullable=False),
        sa.Column("payment_date", sa.Date()),
        sa.Column("amount", sa.Numeric(18, 2), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("source", sa.String(500), nullable=False),
        sa.Column("paid_at", sa.Date()),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["account_id"], ["stock_paper_accounts.id"]),
        sa.UniqueConstraint(
            "account_id",
            "event_key",
            name="uq_stock_paper_receivable_account_event",
        ),
    )
    op.create_index(
        "ix_stock_paper_receivables_account_id",
        "stock_paper_receivables",
        ["account_id"],
    )
    op.create_index(
        "ix_stock_paper_receivables_stock_code",
        "stock_paper_receivables",
        ["stock_code"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_stock_paper_receivables_stock_code",
        table_name="stock_paper_receivables",
    )
    op.drop_index(
        "ix_stock_paper_receivables_account_id",
        table_name="stock_paper_receivables",
    )
    op.drop_table("stock_paper_receivables")
    with op.batch_alter_table("stock_paper_signals") as batch:
        batch.drop_column("order_state")
