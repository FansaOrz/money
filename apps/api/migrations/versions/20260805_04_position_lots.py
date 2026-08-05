"""persist T+1 FIFO position lots in paper and broker ledgers

Revision ID: 20260805_04
Revises: 20260805_03
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260805_04"
down_revision = "20260805_03"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("stock_paper_positions") as batch:
        batch.add_column(
            sa.Column(
                "lots", sa.JSON(), nullable=False, server_default="[]"
            )
        )
    with op.batch_alter_table("stock_paper_trades") as batch:
        batch.add_column(
            sa.Column(
                "lot_consumption", sa.JSON(), nullable=False, server_default="[]"
            )
        )
    with op.batch_alter_table("broker_account_ledgers") as batch:
        batch.add_column(
            sa.Column(
                "position_lots", sa.JSON(), nullable=False, server_default="{}"
            )
        )
    with op.batch_alter_table("broker_fills") as batch:
        batch.add_column(
            sa.Column(
                "lot_consumption", sa.JSON(), nullable=False, server_default="[]"
            )
        )

    connection = op.get_bind()
    positions = sa.table(
        "stock_paper_positions",
        sa.column("id", sa.Integer()),
        sa.column("shares", sa.Numeric()),
        sa.column("cost", sa.Numeric()),
        sa.column("lots", sa.JSON()),
    )
    for row in connection.execute(
        sa.select(positions.c.id, positions.c.shares, positions.c.cost)
    ).all():
        connection.execute(
            sa.update(positions)
            .where(positions.c.id == row.id)
            .values(
                lots=[
                    {
                        "acquired_date": "1970-01-01",
                        "sellable_date": "1970-01-02",
                        "shares": float(row.shares),
                        "total_cost": float(row.cost),
                        "source": "migration:legacy_aggregate_position",
                    }
                ]
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("broker_fills") as batch:
        batch.drop_column("lot_consumption")
    with op.batch_alter_table("broker_account_ledgers") as batch:
        batch.drop_column("position_lots")
    with op.batch_alter_table("stock_paper_trades") as batch:
        batch.drop_column("lot_consumption")
    with op.batch_alter_table("stock_paper_positions") as batch:
        batch.drop_column("lots")
