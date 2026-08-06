"""策略版本生命周期与不可绕过的运行/投资双重门禁。"""

from __future__ import annotations

import math
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.orm import Session

from app.models import AuditLog, StrategyTransition, StrategyVersion
from app.services import strategy_mandate


ALLOWED_TRANSITIONS = {
    "research": {"operational_validated", "investment_validated", "retired"},
    "operational_validated": {
        "paper_operational_validation",
        "investment_validated",
        "retired",
    },
    # 运行模拟不能直接 approved/live；补充全新投资证据后才可转投资验证。
    "paper_operational_validation": {"investment_validated", "retired"},
    "investment_validated": {"paper", "retired"},
    "paper": {"approved", "retired"},
    "approved": {"live", "retired"},
    "live": {"retired"},
    "retired": set(),
    # 仅为迁移前历史记录保留；不能再新建 validated。
    "validated": {"operational_validated", "retired"},
}


def _is_governed_stock_strategy(params: dict[str, object]) -> bool:
    """V4 及其后续规则股票版本必须使用同一套冻结证据门禁。"""
    model = str(params.get("model_version") or "")
    if not model.startswith("stock_rules_v"):
        return False
    suffix = model.removeprefix("stock_rules_v")
    return suffix.isdigit() and int(suffix) >= 4


def _number(evidence: dict[str, object], key: str) -> float | None:
    value = evidence.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    result = float(value)
    return result if math.isfinite(result) else None


def _check(
    results: dict[str, dict[str, Any]],
    failures: list[str],
    *,
    key: str,
    actual: Any,
    expected: Any,
    passed: bool,
    message: str,
) -> None:
    results[key] = {"actual": actual, "expected": expected, "passed": passed}
    if not passed:
        failures.append(message)


def evaluate_gates(
    from_status: str,
    to_status: str,
    evidence: dict[str, object],
    mandate: dict[str, Any],
) -> tuple[bool, list[str], dict[str, dict[str, Any]]]:
    """返回通过状态、失败原因和逐项实际值/阈值，供永久审计。"""

    del from_status
    failures: list[str] = []
    results: dict[str, dict[str, Any]] = {}
    if to_status == "operational_validated":
        coverage = _number(evidence, "data_coverage")
        evaluations = evidence.get(
            "operational_validation_evaluations",
            evidence.get("holdout_evaluations"),
        )
        folds = evidence.get("walkforward_folds")
        sharpe = _number(evidence, "holdout_sharpe")
        trade_count = evidence.get("holdout_trade_count")
        turnover = _number(evidence, "holdout_turnover")
        _check(
            results,
            failures,
            key="data_coverage",
            actual=coverage,
            expected=">=0.95",
            passed=coverage is not None and coverage >= 0.95,
            message="数据覆盖低于95%",
        )
        _check(
            results,
            failures,
            key="operational_validation_evaluations",
            actual=evaluations,
            expected="==1",
            passed=evaluations == 1,
            message="运行链路验证段必须且只能评估一次",
        )
        _check(
            results,
            failures,
            key="walkforward_folds",
            actual=folds,
            expected=">=3",
            passed=isinstance(folds, int)
            and not isinstance(folds, bool)
            and folds >= 3,
            message="purged walk-forward 少于3折",
        )
        _check(
            results,
            failures,
            key="holdout_sharpe_present",
            actual=sharpe,
            expected="finite number（仅证明已计算，不证明Alpha）",
            passed=sharpe is not None,
            message="缺少留出集 Sharpe",
        )
        _check(
            results,
            failures,
            key="holdout_trade_count",
            actual=trade_count,
            expected=">=1",
            passed=(
                isinstance(trade_count, int)
                and not isinstance(trade_count, bool)
                and trade_count >= 1
            ),
            message="留出集没有任何真实模拟成交，不能验证交易链路",
        )
        _check(
            results,
            failures,
            key="holdout_turnover",
            actual=turnover,
            expected=">0",
            passed=turnover is not None and turnover > 0,
            message="留出集换手率为零，策略实际只持有现金",
        )
        _check(
            results,
            failures,
            key="validation_scope",
            actual=evidence.get("validation_scope"),
            expected="operational_only",
            passed=evidence.get("validation_scope") == "operational_only",
            message="运行验证证据必须明确标记 operational_only",
        )
    elif to_status == "paper_operational_validation":
        _check(
            results,
            failures,
            key="experiment_snapshot_complete",
            actual=evidence.get("experiment_snapshot_complete"),
            expected=True,
            passed=evidence.get("experiment_snapshot_complete") is True,
            message="实验快照不完整",
        )
        _check(
            results,
            failures,
            key="investment_approval_eligible",
            actual=mandate.get("investment_approval_eligible"),
            expected=False,
            passed=mandate.get("investment_approval_eligible") is False,
            message="运行模拟必须绑定不可投资批准的任务书",
        )
    elif to_status == "investment_validated":
        thresholds = mandate.get("validation_thresholds")
        eligible = mandate.get("investment_approval_eligible") is True
        _check(
            results,
            failures,
            key="investment_mandate",
            actual=eligible,
            expected=True,
            passed=eligible and isinstance(thresholds, dict),
            message="缺少具备批准资格的投资任务书或有效性阈值",
        )
        if not isinstance(thresholds, dict):
            thresholds = {}
        checks = (
            ("data_coverage", "min_data_coverage", ">=", "数据覆盖未达投资门槛"),
            (
                "walkforward_folds",
                "min_walkforward_folds",
                ">=",
                "走步折数未达投资门槛",
            ),
            ("holdout_sharpe", "min_holdout_sharpe", ">=", "留出集 Sharpe 未达门槛"),
            (
                "net_excess_return",
                "min_net_excess_return",
                ">",
                "扣费后超额收益必须为正",
            ),
            ("active_sharpe", "min_active_sharpe", ">=", "主动 Sharpe 未达门槛"),
            (
                "active_return_ci_lower",
                "min_active_return_ci_lower",
                ">",
                "主动收益置信区间未严格高于零",
            ),
            (
                "regression_alpha_ci_lower",
                "min_regression_alpha_ci_lower",
                ">",
                "市场/风格回归 Alpha 置信区间未严格高于零",
            ),
            ("max_drawdown", "max_drawdown", ">=", "最大回撤突破任务书"),
            ("rank_ic_mean", "min_rank_ic_mean", ">=", "Rank IC 均值未达门槛"),
            ("rank_icir", "min_rank_icir", ">=", "Rank ICIR 未达门槛"),
            ("rank_ic_p_value", "max_rank_ic_p_value", "<=", "Rank IC 显著性未达门槛"),
            (
                "rank_ic_ci_lower",
                "min_rank_ic_ci_lower",
                ">",
                "Rank IC 置信区间未严格高于零",
            ),
            (
                "quintile_monotonicity",
                "min_quintile_monotonicity",
                ">=",
                "五档单调性未达门槛",
            ),
            (
                "top_bottom_spread",
                "min_top_bottom_spread",
                ">",
                "头尾档净收益差必须为正",
            ),
            (
                "deflated_sharpe_probability",
                "min_deflated_sharpe_probability",
                ">=",
                "Deflated Sharpe 概率未达门槛",
            ),
            (
                "probabilistic_sharpe_probability",
                "min_probabilistic_sharpe_probability",
                ">=",
                "Probabilistic Sharpe 概率未达门槛",
            ),
            (
                "probability_backtest_overfitting",
                "max_probability_backtest_overfitting",
                "<=",
                "回测过拟合概率超过门槛",
            ),
            (
                "multiple_testing_fdr",
                "max_multiple_testing_fdr",
                "<=",
                "多重检验FDR超过门槛",
            ),
            (
                "cost_2x_excess_return",
                "min_cost_2x_excess_return",
                ">",
                "成本翻倍后超额收益必须为正",
            ),
            (
                "max_single_period_alpha_contribution",
                "max_single_period_alpha_contribution",
                "<=",
                "Alpha 过度依赖单一时期",
            ),
            (
                "worst_regime_excess_return",
                "min_worst_regime_excess_return",
                ">=",
                "最差行情状态超额收益低于门槛",
            ),
            (
                "worst_year_excess_return",
                "min_worst_year_excess_return",
                ">=",
                "最差年度超额收益低于门槛",
            ),
        )
        for evidence_key, threshold_key, operator, message in checks:
            actual = _number(evidence, evidence_key)
            threshold = thresholds.get(threshold_key)
            valid_threshold = isinstance(threshold, (int, float)) and not isinstance(
                threshold, bool
            )
            if actual is None or not valid_threshold:
                passed = False
            elif operator == ">=":
                passed = actual >= float(threshold)
            elif operator == "<=":
                passed = actual <= float(threshold)
            else:
                passed = actual > float(threshold)
            _check(
                results,
                failures,
                key=evidence_key,
                actual=actual,
                expected=f"{operator}{threshold}",
                passed=passed,
                message=message,
            )
        _check(
            results,
            failures,
            key="holdout_evaluations",
            actual=evidence.get("holdout_evaluations"),
            expected="==1",
            passed=evidence.get("holdout_evaluations") == 1,
            message="完全留出集必须且只能评估一次",
        )
        _check(
            results,
            failures,
            key="robustness_passed",
            actual=evidence.get("robustness_passed"),
            expected=True,
            passed=evidence.get("robustness_passed") is True,
            message="参数、成本、容量与数据扰动稳健性门禁未通过",
        )
        _check(
            results,
            failures,
            key="benchmark_kind",
            actual=evidence.get("benchmark_kind"),
            expected=mandate.get("benchmark", {}).get("primary"),
            passed=(
                evidence.get("benchmark_kind")
                == mandate.get("benchmark", {}).get("primary")
            ),
            message="正式证据未使用任务书指定官方全收益基准",
        )
    elif to_status == "paper":
        _check(
            results,
            failures,
            key="experiment_snapshot_complete",
            actual=evidence.get("experiment_snapshot_complete"),
            expected=True,
            passed=evidence.get("experiment_snapshot_complete") is True,
            message="实验快照不完整",
        )
        _check(
            results,
            failures,
            key="investment_validation_passed",
            actual=evidence.get("investment_validation_passed"),
            expected=True,
            passed=evidence.get("investment_validation_passed") is True,
            message="缺少投资有效性通过证据",
        )
    elif to_status == "approved":
        checks = (
            (
                "paper_trading_days",
                evidence.get("paper_trading_days"),
                isinstance(evidence.get("paper_trading_days"), int)
                and not isinstance(evidence.get("paper_trading_days"), bool)
                and int(evidence["paper_trading_days"]) >= 42,
                ">=42",
                "前向模拟不足42个交易日",
            ),
            (
                "reconciliation_clean",
                evidence.get("reconciliation_clean"),
                evidence.get("reconciliation_clean") is True,
                True,
                "账户对账未通过",
            ),
            (
                "operational_failures",
                evidence.get("operational_failures"),
                evidence.get("operational_failures") == 0,
                0,
                "前向运行仍有失败",
            ),
            (
                "investment_validation_passed",
                evidence.get("investment_validation_passed"),
                evidence.get("investment_validation_passed") is True,
                True,
                "投资有效性未通过",
            ),
        )
        for key, actual, passed, expected, message in checks:
            _check(
                results,
                failures,
                key=key,
                actual=actual,
                expected=expected,
                passed=passed,
                message=message,
            )
    elif to_status == "live":
        checks = (
            ("manual_approval", "缺少人工实盘批准"),
            ("broker_reconciliation_ready", "券商对账尚未就绪"),
            ("kill_switch_tested", "紧急停止未演练"),
        )
        for key, message in checks:
            _check(
                results,
                failures,
                key=key,
                actual=evidence.get(key),
                expected=True,
                passed=evidence.get(key) is True,
                message=message,
            )
    return not failures, failures, results


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
    mandate_failures = strategy_mandate.validate_mandate(
        dict(version.mandate or {}), version.mandate_sha256
    )
    if to_status == "retired":
        mandate_failures = []
    approved, failures, gate_results = evaluate_gates(
        from_status, to_status, evidence, dict(version.mandate or {})
    )
    failures = mandate_failures + failures
    params = dict(version.params or {})
    if to_status in {
        "operational_validated",
        "investment_validated",
    } and _is_governed_stock_strategy(params):
        for key in (
            "benchmark_kind",
            "benchmark_code",
            "benchmark_curve_sha256",
            "benchmark_start_date",
            "benchmark_end_date",
            "benchmark_curve_points",
            "strategy_curve_sha256",
            "benchmark_return_kind",
            "benchmark_source_hashes",
            "benchmark_source_files",
            "comparator_metrics",
            "limit_data_coverage",
        ):
            if evidence.get(key) in (None, ""):
                failures.append(f"股票验证证据缺少冻结字段：{key}")
        expected_hash = params.get("validation_sha256")
        if not expected_hash or evidence.get("validation_sha256") != expected_hash:
            failures.append("验证证据哈希与策略冻结快照不一致")
        trusted_generators = {"stock_validation.run_stock_walk_forward"}
        if to_status == "operational_validated":
            trusted_generators.add(
                "scripts.run_strategy_v13_operational_shadow.run_development_replay"
            )
        if evidence.get("generated_by") not in trusted_generators:
            failures.append("股票验证证据不是由系统走步验证生成")
        stored_key = (
            "operational_validation_evidence"
            if to_status == "operational_validated"
            else "investment_validation_evidence"
        )
        stored_evidence = params.get(stored_key)
        if not isinstance(stored_evidence, dict):
            failures.append(f"策略冻结快照缺少 {stored_key}")
        else:
            for key, value in stored_evidence.items():
                if evidence.get(key) != value:
                    failures.append(f"调用证据与系统冻结值不一致：{key}")
    if (
        to_status in {"paper_operational_validation", "paper"}
        and _is_governed_stock_strategy(params)
        and evidence.get("validation_sha256") != params.get("validation_sha256")
    ):
        failures.append("前向版本未绑定已冻结的验证证据")
    approved = approved and not failures
    db.add(
        StrategyTransition(
            strategy_version_id=version.id,
            from_status=from_status,
            to_status=to_status,
            gates={
                **evidence,
                "mandate_sha256": version.mandate_sha256,
                "gate_results": gate_results,
                "failures": failures,
            },
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
            detail={
                "from": from_status,
                "to": to_status,
                "reason": reason,
                "mandate_sha256": version.mandate_sha256,
                "gate_results": gate_results,
            },
            created_at=datetime.now(UTC),
        )
    )
    db.commit()
    db.refresh(version)
    return version
