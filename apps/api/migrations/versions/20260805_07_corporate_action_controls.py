"""add restricted assets, dividend tax and corporate action review

Revision ID: 20260805_07
Revises: 20260805_06
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260805_07"
down_revision = "20260805_06"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("stock_paper_positions") as batch:
        batch.add_column(
            sa.Column(
                "status",
                sa.String(30),
                nullable=False,
                server_default="tradable",
            )
        )
        batch.add_column(
            sa.Column(
                "restricted_shares",
                sa.Numeric(24, 6),
                nullable=False,
                server_default="0",
            )
        )
        batch.add_column(
            sa.Column(
                "restricted_value",
                sa.Numeric(18, 2),
                nullable=False,
                server_default="0",
            )
        )
        batch.add_column(sa.Column("restriction_reason", sa.String(500)))
        batch.add_column(sa.Column("sellable_after", sa.Date()))

    op.create_table(
        "stock_paper_dividend_tax_liabilities",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "account_id",
            sa.Integer(),
            sa.ForeignKey("stock_paper_accounts.id"),
            nullable=False,
        ),
        sa.Column("stock_code", sa.String(10), nullable=False),
        sa.Column("event_key", sa.String(160), nullable=False),
        sa.Column("lot_id", sa.String(64), nullable=False),
        sa.Column("acquired_date", sa.Date(), nullable=False),
        sa.Column("entitlement_date", sa.Date(), nullable=False),
        sa.Column("remaining_shares", sa.Numeric(24, 6), nullable=False),
        sa.Column("gross_cash_per_share", sa.Numeric(18, 6), nullable=False),
        sa.Column(
            "withheld_at_payment",
            sa.Numeric(18, 2),
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "tax_paid",
            sa.Numeric(18, 2),
            nullable=False,
            server_default="0",
        ),
        sa.Column("rule_version", sa.String(100), nullable=False),
        sa.Column(
            "status",
            sa.String(20),
            nullable=False,
            server_default="open",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "account_id",
            "event_key",
            "lot_id",
            name="uq_stock_paper_dividend_tax_event_lot",
        ),
    )
    op.create_index(
        "ix_stock_paper_dividend_tax_liabilities_account_id",
        "stock_paper_dividend_tax_liabilities",
        ["account_id"],
    )
    op.create_index(
        "ix_stock_paper_dividend_tax_liabilities_stock_code",
        "stock_paper_dividend_tax_liabilities",
        ["stock_code"],
    )

    op.create_table(
        "corporate_action_review_cases",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "record_id",
            sa.Integer(),
            sa.ForeignKey("quant_data_records.id"),
        ),
        sa.Column("code", sa.String(20), nullable=False),
        sa.Column("event_key", sa.String(160), nullable=False),
        sa.Column("issue_type", sa.String(50), nullable=False),
        sa.Column(
            "status",
            sa.String(20),
            nullable=False,
            server_default="open",
        ),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column(
            "conservative_value",
            sa.Numeric(20, 6),
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "evidence",
            sa.JSON(),
            nullable=False,
            server_default="{}",
        ),
        sa.Column(
            "resolution",
            sa.JSON(),
            nullable=False,
            server_default="{}",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("resolved_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint(
            "code",
            "event_key",
            "issue_type",
            name="uq_corporate_action_review_issue",
        ),
    )
    op.create_index(
        "ix_corporate_action_review_cases_record_id",
        "corporate_action_review_cases",
        ["record_id"],
    )
    op.create_index(
        "ix_corporate_action_review_cases_code",
        "corporate_action_review_cases",
        ["code"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_corporate_action_review_cases_code",
        table_name="corporate_action_review_cases",
    )
    op.drop_index(
        "ix_corporate_action_review_cases_record_id",
        table_name="corporate_action_review_cases",
    )
    op.drop_table("corporate_action_review_cases")
    op.drop_index(
        "ix_stock_paper_dividend_tax_liabilities_stock_code",
        table_name="stock_paper_dividend_tax_liabilities",
    )
    op.drop_index(
        "ix_stock_paper_dividend_tax_liabilities_account_id",
        table_name="stock_paper_dividend_tax_liabilities",
    )
    op.drop_table("stock_paper_dividend_tax_liabilities")
    with op.batch_alter_table("stock_paper_positions") as batch:
        for column in (
            "sellable_after",
            "restriction_reason",
            "restricted_value",
            "restricted_shares",
            "status",
        ):
            batch.drop_column(column)
