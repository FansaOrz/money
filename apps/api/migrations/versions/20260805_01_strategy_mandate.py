"""strategy mandate and explicit operational-paper state

Revision ID: 20260805_01
Revises: 20260804_02
"""

from __future__ import annotations

import hashlib
import json

from alembic import op
import sqlalchemy as sa


revision = "20260805_01"
down_revision = "20260804_02"
branch_labels = None
depends_on = None


def _canonical(value: dict) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _legacy_operational_mandate(row) -> dict:
    return {
        "mandate_version": "operational-paper-validation-v1",
        "name": f"{row.name}-历史运行链路验证任务书",
        "investor": "个人研究账户",
        "asset_class": "中国A股或基金",
        "direction": "long_only",
        "validation_scope": "operational_only",
        "investment_approval_eligible": False,
        "initial_capital_cny": float(row.initial_capital),
        "rebalance_days": int(row.rebalance_interval),
        "target_holdings": int(row.top_n),
        "purpose": (
            "迁移前历史版本；仅保留研究和运行复现资格，不构成Alpha或实盘批准"
        ),
        "stop_conditions": [
            "关键数据未就绪",
            "账本或对账异常",
            "运行证据不完整",
        ],
    }


def upgrade() -> None:
    with op.batch_alter_table("strategy_versions") as batch:
        batch.add_column(sa.Column("mandate", sa.JSON(), nullable=True))
        batch.add_column(sa.Column("mandate_sha256", sa.String(64), nullable=True))

    table = sa.table(
        "strategy_versions",
        sa.column("id", sa.Integer()),
        sa.column("name", sa.String()),
        sa.column("initial_capital", sa.Numeric()),
        sa.column("rebalance_interval", sa.Integer()),
        sa.column("top_n", sa.Integer()),
        sa.column("status", sa.String()),
        sa.column("mandate", sa.JSON()),
        sa.column("mandate_sha256", sa.String()),
    )
    connection = op.get_bind()
    rows = connection.execute(
        sa.select(
            table.c.id,
            table.c.name,
            table.c.initial_capital,
            table.c.rebalance_interval,
            table.c.top_n,
            table.c.status,
        )
    ).all()
    for row in rows:
        mandate = _legacy_operational_mandate(row)
        status = row.status
        if status == "paper":
            status = "paper_operational_validation"
        elif status == "validated":
            status = "operational_validated"
        connection.execute(
            sa.update(table)
            .where(table.c.id == row.id)
            .values(
                mandate=mandate,
                mandate_sha256=hashlib.sha256(_canonical(mandate).encode()).hexdigest(),
                status=status,
            )
        )

    with op.batch_alter_table("strategy_versions") as batch:
        batch.alter_column("mandate", nullable=False)
        batch.alter_column("mandate_sha256", nullable=False)


def downgrade() -> None:
    connection = op.get_bind()
    table = sa.table(
        "strategy_versions",
        sa.column("status", sa.String()),
    )
    connection.execute(
        sa.update(table)
        .where(table.c.status == "paper_operational_validation")
        .values(status="paper")
    )
    connection.execute(
        sa.update(table)
        .where(table.c.status == "operational_validated")
        .values(status="validated")
    )
    with op.batch_alter_table("strategy_versions") as batch:
        batch.drop_column("mandate_sha256")
        batch.drop_column("mandate")
