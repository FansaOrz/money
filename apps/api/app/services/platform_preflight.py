"""从数据库和代码状态生成高风险操作预检查，不接受调用方自报健康状态。"""

from __future__ import annotations

import subprocess
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import (
    BrokerAccountLedger,
    DataSourceSLAState,
    StockPaperNavDaily,
    StrategyVersion,
)
from app.services.high_risk_preflight import evaluate_preflight


def _workspace_clean() -> bool:
    if not Path(".git").exists():
        return True
    try:
        return not subprocess.run(
            ["git", "status", "--porcelain"],
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return False


def evaluate_system_preflight(
    db: Session,
    *,
    operation: str,
    target: str,
    impact: str,
    idempotency_key: str,
    confirmation_digest: str | None = None,
) -> dict[str, object]:
    required = list(
        db.scalars(
            select(DataSourceSLAState).where(DataSourceSLAState.required.is_(True))
        ).all()
    )
    data_fresh = bool(required) and all(row.status == "success" for row in required)
    latest_version = db.scalar(
        select(StrategyVersion).order_by(StrategyVersion.id.desc()).limit(1)
    )
    evidence_consistent = (
        latest_version is not None
        and bool(latest_version.mandate_sha256)
        and bool(dict(latest_version.params or {}).get("git_sha"))
    )
    reconciliation_breaks = int(
        db.scalar(
            select(func.count(BrokerAccountLedger.account)).where(
                BrokerAccountLedger.reconciliation_status != "clean"
            )
        )
        or 0
    )
    cash_breaks = int(
        db.scalar(
            select(func.count(StockPaperNavDaily.id)).where(
                func.abs(StockPaperNavDaily.cash_conservation_error) > 0.01
            )
        )
        or 0
    )
    return evaluate_preflight(
        operation=operation,
        target=target,
        impact=impact,
        data_fresh=data_fresh,
        clean_workspace=_workspace_clean(),
        evidence_consistent=evidence_consistent,
        ledger_balanced=reconciliation_breaks == 0 and cash_breaks == 0,
        idempotency_key=idempotency_key,
        confirmation_digest=confirmation_digest,
    )
