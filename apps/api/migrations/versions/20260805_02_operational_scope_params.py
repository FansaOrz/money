"""mark migrated strategy params as operational-only

Revision ID: 20260805_02
Revises: 20260805_01
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260805_02"
down_revision = "20260805_01"
branch_labels = None
depends_on = None

_BLOCKER = (
    "迁移前历史版本仅保留研究/运行复现资格；必须创建绑定投资任务书的新版本，"
    "并通过净超额、主动风险、IC显著性、DSR/PBO和成本压力门禁"
)


def upgrade() -> None:
    table = sa.table(
        "strategy_versions",
        sa.column("id", sa.Integer()),
        sa.column("params", sa.JSON()),
        sa.column("mandate", sa.JSON()),
    )
    connection = op.get_bind()
    rows = connection.execute(
        sa.select(table.c.id, table.c.params, table.c.mandate)
    ).all()
    for row in rows:
        mandate = dict(row.mandate or {})
        if mandate.get("validation_scope") != "operational_only":
            continue
        params = dict(row.params or {})
        params.update(
            {
                "validation_scope": "operational_only",
                "investment_approval_eligible": False,
                "approval_blocker": _BLOCKER,
            }
        )
        connection.execute(
            sa.update(table).where(table.c.id == row.id).values(params=params)
        )


def downgrade() -> None:
    table = sa.table(
        "strategy_versions",
        sa.column("id", sa.Integer()),
        sa.column("params", sa.JSON()),
    )
    connection = op.get_bind()
    rows = connection.execute(sa.select(table.c.id, table.c.params)).all()
    for row in rows:
        params = dict(row.params or {})
        params.pop("validation_scope", None)
        params.pop("investment_approval_eligible", None)
        params.pop("approval_blocker", None)
        connection.execute(
            sa.update(table).where(table.c.id == row.id).values(params=params)
        )
