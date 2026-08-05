"""add explicit open and close prices to TCA records

Revision ID: 20260805_17
Revises: 20260805_16
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260805_17"
down_revision = "20260805_16"
branch_labels = None
depends_on = None


def _add_if_missing(table: str, column: str) -> None:
    inspector = sa.inspect(op.get_bind())
    existing = {item["name"] for item in inspector.get_columns(table)}
    if column not in existing:
        with op.batch_alter_table(table) as batch:
            batch.add_column(sa.Column(column, sa.Numeric(20, 6)))


def upgrade() -> None:
    tables = set(sa.inspect(op.get_bind()).get_table_names())
    for table in ("stock_paper_trades", "broker_fills"):
        if table in tables:
            _add_if_missing(table, "open_price")
            _add_if_missing(table, "close_price")


def downgrade() -> None:
    tables = set(sa.inspect(op.get_bind()).get_table_names())
    for table in ("broker_fills", "stock_paper_trades"):
        if table in tables:
            with op.batch_alter_table(table) as batch:
                batch.drop_column("close_price")
                batch.drop_column("open_price")
