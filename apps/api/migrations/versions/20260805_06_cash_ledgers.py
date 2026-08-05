"""add settled, frozen and receivable cash audit ledgers

Revision ID: 20260805_06
Revises: 20260805_05
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260805_06"
down_revision = "20260805_05"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("stock_paper_accounts") as batch:
        batch.add_column(
            sa.Column(
                "frozen_cash",
                sa.Numeric(18, 2),
                nullable=False,
                server_default="0",
            )
        )
        batch.add_column(
            sa.Column(
                "settled_cash",
                sa.Numeric(18, 2),
                nullable=False,
                server_default="0",
            )
        )
    op.execute("UPDATE stock_paper_accounts SET settled_cash = cash")

    with op.batch_alter_table("stock_paper_nav_daily") as batch:
        batch.add_column(
            sa.Column(
                "frozen_cash",
                sa.Numeric(18, 2),
                nullable=False,
                server_default="0",
            )
        )
        batch.add_column(
            sa.Column(
                "receivable_cash",
                sa.Numeric(18, 2),
                nullable=False,
                server_default="0",
            )
        )
        batch.add_column(
            sa.Column(
                "settled_cash",
                sa.Numeric(18, 2),
                nullable=False,
                server_default="0",
            )
        )
        batch.add_column(
            sa.Column(
                "cash_interest",
                sa.Numeric(18, 2),
                nullable=False,
                server_default="0",
            )
        )
        batch.add_column(
            sa.Column(
                "cash_ledger",
                sa.JSON(),
                nullable=False,
                server_default="{}",
            )
        )
        batch.add_column(
            sa.Column(
                "cash_conservation_error",
                sa.Numeric(18, 8),
                nullable=False,
                server_default="0",
            )
        )
    op.execute("UPDATE stock_paper_nav_daily SET settled_cash = cash")

    op.create_table(
        "stock_paper_cash_settlements",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "account_id",
            sa.Integer(),
            sa.ForeignKey("stock_paper_accounts.id"),
            nullable=False,
        ),
        sa.Column("trade_date", sa.Date(), nullable=False),
        sa.Column("settle_date", sa.Date(), nullable=False),
        sa.Column("amount", sa.Numeric(18, 2), nullable=False),
        sa.Column("reference", sa.String(160), nullable=False),
        sa.Column(
            "status",
            sa.String(20),
            nullable=False,
            server_default="pending",
        ),
        sa.Column("settled_at", sa.Date()),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "account_id",
            "reference",
            name="uq_stock_paper_cash_settlement_reference",
        ),
    )
    op.create_index(
        "ix_stock_paper_cash_settlements_account_id",
        "stock_paper_cash_settlements",
        ["account_id"],
    )
    op.create_index(
        "ix_stock_paper_cash_settlements_trade_date",
        "stock_paper_cash_settlements",
        ["trade_date"],
    )
    op.create_index(
        "ix_stock_paper_cash_settlements_settle_date",
        "stock_paper_cash_settlements",
        ["settle_date"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_stock_paper_cash_settlements_settle_date",
        table_name="stock_paper_cash_settlements",
    )
    op.drop_index(
        "ix_stock_paper_cash_settlements_trade_date",
        table_name="stock_paper_cash_settlements",
    )
    op.drop_index(
        "ix_stock_paper_cash_settlements_account_id",
        table_name="stock_paper_cash_settlements",
    )
    op.drop_table("stock_paper_cash_settlements")
    with op.batch_alter_table("stock_paper_nav_daily") as batch:
        for column in (
            "cash_conservation_error",
            "cash_ledger",
            "cash_interest",
            "settled_cash",
            "receivable_cash",
            "frozen_cash",
        ):
            batch.drop_column(column)
    with op.batch_alter_table("stock_paper_accounts") as batch:
        batch.drop_column("settled_cash")
        batch.drop_column("frozen_cash")
