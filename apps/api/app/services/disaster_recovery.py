"""PostgreSQL restore/PITR 演练计划和恢复后账本不变量检查。"""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import BrokerFill, BrokerOrder, StockPaperNavDaily


RPO_RTO_POLICY = {
    "market_data": {"rpo_minutes": 1440, "rto_minutes": 240},
    "research_experiments": {"rpo_minutes": 1440, "rto_minutes": 240},
    "orders_and_fills": {"rpo_minutes": 1, "rto_minutes": 30},
    "account_ledger": {"rpo_minutes": 1, "rto_minutes": 30},
}


def pitr_readiness(*, archive_mode: str, archive_command: str, wal_level: str) -> dict:
    reasons: list[str] = []
    if archive_mode.lower() not in {"on", "always"}:
        reasons.append("archive_mode 未开启")
    if not archive_command.strip():
        reasons.append("archive_command 未配置")
    if wal_level.lower() not in {"replica", "logical"}:
        reasons.append("wal_level 不支持 PITR")
    return {"ok": not reasons, "reasons": reasons}


def restore_postgresql_dump(
    *,
    dump_path: Path,
    target_dsn_without_password: str,
    pgpass_file: Path,
    timeout_seconds: int = 1800,
) -> dict[str, object]:
    if pgpass_file.stat().st_mode & 0o077:
        raise PermissionError("PGPASSFILE 权限必须为 0600")
    started = time.monotonic()
    command = [
        "pg_restore",
        "--clean",
        "--if-exists",
        "--exit-on-error",
        "--no-password",
        "--dbname",
        target_dsn_without_password,
        str(dump_path),
    ]
    subprocess.run(
        command,
        check=True,
        timeout=timeout_seconds,
        env={**os.environ, "PGPASSFILE": str(pgpass_file)},
        capture_output=True,
    )
    return {"ok": True, "elapsed_seconds": time.monotonic() - started}


def verify_ledger_invariants(db: Session) -> dict[str, object]:
    overfilled = int(
        db.scalar(
            select(func.count(BrokerOrder.id)).where(
                BrokerOrder.filled_quantity > BrokerOrder.quantity
            )
        )
        or 0
    )
    orphan_fills = int(
        db.scalar(
            select(func.count(BrokerFill.id))
            .outerjoin(BrokerOrder, BrokerFill.order_id == BrokerOrder.id)
            .where(BrokerOrder.id.is_(None))
        )
        or 0
    )
    bad_nav = int(
        db.scalar(
            select(func.count(StockPaperNavDaily.id)).where(
                func.abs(StockPaperNavDaily.cash_conservation_error) > 0.01
            )
        )
        or 0
    )
    return {
        "ok": overfilled == 0 and orphan_fills == 0 and bad_nav == 0,
        "overfilled_orders": overfilled,
        "orphan_fills": orphan_fills,
        "cash_conservation_breaks": bad_nav,
    }
