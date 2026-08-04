"""策略版本生命周期与不可绕过的晋级门禁。"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy.orm import Session

from app.models import AuditLog, StrategyTransition, StrategyVersion

ALLOWED_TRANSITIONS = {
    "research": {"validated", "retired"},
    "validated": {"paper", "retired"},
    "paper": {"approved", "retired"},
    "approved": {"live", "retired"},
    "live": {"retired"},
    "retired": set(),
}


def evaluate_gates(
    from_status: str, to_status: str, evidence: dict[str, object]
) -> tuple[bool, list[str]]:
    failures: list[str] = []
    if to_status == "validated":
        if float(evidence.get("data_coverage", 0.0)) < 0.95:
            failures.append("数据覆盖低于95%")
        if int(evidence.get("holdout_evaluations", 0)) != 1:
            failures.append("完全留出集必须且只能评估一次")
        if int(evidence.get("walkforward_folds", 0)) < 3:
            failures.append("purged walk-forward 少于3折")
        if evidence.get("holdout_sharpe") is None:
            failures.append("缺少留出集 Sharpe")
    elif to_status == "paper":
        if not bool(evidence.get("experiment_snapshot_complete")):
            failures.append("实验快照不完整")
    elif to_status == "approved":
        if int(evidence.get("paper_trading_days", 0)) < 42:
            failures.append("前向模拟不足42个交易日")
        if not bool(evidence.get("reconciliation_clean")):
            failures.append("账户对账未通过")
        if int(evidence.get("operational_failures", 1)) > 0:
            failures.append("前向运行仍有失败")
    elif to_status == "live":
        if not bool(evidence.get("manual_approval")):
            failures.append("缺少人工实盘批准")
        if not bool(evidence.get("broker_reconciliation_ready")):
            failures.append("券商对账尚未就绪")
        if not bool(evidence.get("kill_switch_tested")):
            failures.append("紧急停止未演练")
    return not failures, failures


def transition(
    db: Session,
    strategy_version_id: int,
    to_status: str,
    *,
    evidence: dict[str, object],
    actor: str,
    reason: str,
) -> StrategyVersion:
    version = db.get(StrategyVersion, strategy_version_id)
    if version is None:
        raise ValueError("策略版本不存在")
    from_status = version.status
    if to_status not in ALLOWED_TRANSITIONS.get(from_status, set()):
        raise ValueError(f"不允许 {from_status} → {to_status}")
    approved, failures = evaluate_gates(from_status, to_status, evidence)
    params = dict(version.params or {})
    if (
        to_status == "validated"
        and params.get("model_version") == "stock_rules_v4"
    ):
        expected_hash = params.get("validation_sha256")
        if not expected_hash or evidence.get("validation_sha256") != expected_hash:
            failures.append("验证证据哈希与策略冻结快照不一致")
        if evidence.get("generated_by") != "stock_validation.run_stock_walk_forward":
            failures.append("股票验证证据不是由系统走步验证生成")
        approved = not failures
    if (
        to_status == "paper"
        and params.get("model_version") == "stock_rules_v4"
        and evidence.get("validation_sha256") != params.get("validation_sha256")
    ):
        failures.append("前向版本未绑定已冻结的验证证据")
        approved = False
    db.add(
        StrategyTransition(
            strategy_version_id=version.id,
            from_status=from_status,
            to_status=to_status,
            gates={**evidence, "failures": failures},
            approved=approved,
            actor=actor,
            reason=reason,
            created_at=datetime.now(UTC),
        )
    )
    if not approved:
        db.commit()
        raise ValueError("晋级门禁未通过：" + "；".join(failures))
    version.status = to_status
    db.add(
        AuditLog(
            actor=actor,
            action="strategy_transition",
            resource_type="strategy_version",
            resource_id=str(version.id),
            detail={"from": from_status, "to": to_status, "reason": reason},
            created_at=datetime.now(UTC),
        )
    )
    db.commit()
    db.refresh(version)
    return version
