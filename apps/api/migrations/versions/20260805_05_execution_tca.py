"""persist execution TCA fields for calibration

Revision ID: 20260805_05
Revises: 20260805_04
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260805_05"
down_revision = "20260805_04"
branch_labels = None
depends_on = None


_TRADE_FIELDS = (
    sa.Column("arrival_price", sa.Numeric(18, 6)),
    sa.Column("decision_price", sa.Numeric(18, 6)),
    sa.Column("market_vwap", sa.Numeric(18, 6)),
    sa.Column("participation_rate", sa.Numeric(12, 8)),
    sa.Column("implementation_shortfall", sa.Numeric(12, 8)),
    sa.Column("recent_volatility", sa.Numeric(12, 8)),
    sa.Column("liquidity_adv", sa.Numeric(24, 6)),
    sa.Column(
        "execution_session",
        sa.String(20),
        nullable=False,
        server_default="open",
    ),
    sa.Column(
        "slippage_model_version",
        sa.String(100),
        nullable=False,
        server_default="OPEN_ADV_SQRT_V1",
    ),
)


def upgrade() -> None:
    with op.batch_alter_table("stock_paper_trades") as batch:
        for column in _TRADE_FIELDS:
            batch.add_column(column)
        batch.add_column(
            sa.Column(
                "cost_scenario",
                sa.String(30),
                nullable=False,
                server_default="baseline",
            )
        )
    with op.batch_alter_table("broker_orders") as batch:
        batch.add_column(sa.Column("reference_price", sa.Numeric(20, 6)))
    with op.batch_alter_table("broker_fills") as batch:
        for column in _TRADE_FIELDS:
            batch.add_column(column.copy())


def downgrade() -> None:
    with op.batch_alter_table("broker_fills") as batch:
        for column in reversed(_TRADE_FIELDS):
            batch.drop_column(column.name)
    with op.batch_alter_table("broker_orders") as batch:
        batch.drop_column("reference_price")
    with op.batch_alter_table("stock_paper_trades") as batch:
        batch.drop_column("cost_scenario")
        for column in reversed(_TRADE_FIELDS):
            batch.drop_column(column.name)
