"""创建版本16运行影子；只用开发数据验证链路，不读取正式留出集。"""

from __future__ import annotations

import hashlib
import json
import platform
import subprocess
import sys
from datetime import UTC, date, datetime
from pathlib import Path

from sqlalchemy import select

from app.db.session import SessionLocal
from app.models import (
    AuditLog,
    StockPaperAccount,
    StockPaperRun,
    StrategyVersion,
)
from app.services import (
    stock_backtest,
    stock_paper,
    stock_validation,
    strategy_lifecycle,
    strategy_mandate,
)
from app.services.stock_repository import load_repository

SOURCE_VERSION_ID = 11
REPLAY_FOLDS = (
    (date(2020, 1, 2), date(2020, 4, 30)),
    (date(2020, 5, 6), date(2020, 8, 31)),
    (date(2020, 9, 1), date(2020, 12, 31)),
)
VALIDATION_START = date(2022, 1, 4)
VALIDATION_END = date(2022, 12, 29)
PREVIOUS_SHADOW_NAME = "A股多因子规则V7-版本15运行影子镜像"
GENERATOR = "scripts.run_strategy_v16_operational_shadow.run_development_replay"


def _config(
    *,
    start: date,
    end: date,
    version_id: int,
    factor_weights: dict[str, float],
) -> stock_backtest.BacktestConfig:
    return stock_backtest.BacktestConfig(
        start=start,
        end=end,
        initial_capital=float(stock_paper.INITIAL_CAPITAL),
        top_n=stock_paper.TOP_N,
        max_stock_weight=stock_paper.MAX_STOCK_WEIGHT,
        max_industry_weight=stock_paper.MAX_INDUSTRY_WEIGHT,
        min_avg_amount=stock_paper.MIN_AVG_AMOUNT,
        price_limit=stock_paper.PRICE_LIMIT_COEFFICIENT,
        universe_indices=stock_paper.INDEX_CODES,
        initial_signal=True,
        min_universe_data_coverage=0.95,
        max_volume_participation=stock_paper.MAX_VOLUME_PARTICIPATION,
        minimum_trade_weight=stock_paper.MINIMUM_TRADE_WEIGHT,
        minimum_holdings=stock_paper.MINIMUM_HOLDINGS,
        max_annual_volatility=stock_paper.MAX_ANNUAL_VOLATILITY,
        max_tracking_error=stock_paper.MAX_TRACKING_ERROR,
        benchmark_index="H00906",
        benchmark_required=True,
        benchmark_return_kind="gross_total_return",
        min_limit_data_coverage=0.99,
        adaptive_ic_weights=False,
        factor_weights=factor_weights,
        cost=stock_paper.COST,
        strategy_name=stock_paper.OPERATIONAL_SHADOW_NAME,
        strategy_version_id=version_id,
        strict_file_manifest=False,
    )


def _git_snapshot(root: Path) -> tuple[str, str]:
    git_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
        timeout=5,
    ).stdout.strip()
    status = subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=all"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    ).stdout
    if status.strip():
        raise RuntimeError("工作区存在未提交改动，拒绝创建运行影子")
    return git_sha, hashlib.sha256(status.encode()).hexdigest()


def _evidence(
    validation: dict[str, object],
    *,
    validation_sha256: str,
) -> dict[str, object]:
    fold_metrics = list(validation["folds"])
    holdout = dict(validation["validation"])
    coverages = [
        float(dict(item).get("minimum_data_coverage") or 0.0) for item in fold_metrics
    ]
    coverages.append(float(holdout.get("minimum_data_coverage") or 0.0))
    return {
        "data_coverage": min(coverages),
        "limit_data_coverage": float(
            holdout.get("execution_limit_data_coverage") or 0.0
        ),
        "holdout_evaluations": 0,
        "operational_validation_evaluations": 1,
        "walkforward_folds": len(fold_metrics),
        "holdout_sharpe": holdout.get("sharpe"),
        "holdout_trade_count": holdout.get("trade_count"),
        "holdout_turnover": holdout.get("turnover"),
        "validation_scope": "operational_only",
        "benchmark_kind": holdout.get("benchmark_kind"),
        "benchmark_code": holdout.get("benchmark_code"),
        "benchmark_curve_sha256": holdout.get("benchmark_curve_sha256"),
        "benchmark_start_date": holdout.get("benchmark_start_date"),
        "benchmark_end_date": holdout.get("benchmark_end_date"),
        "benchmark_curve_points": holdout.get("benchmark_curve_points"),
        "strategy_curve_sha256": holdout.get("strategy_curve_sha256"),
        "benchmark_return_kind": holdout.get("benchmark_return_kind"),
        "benchmark_source_hashes": holdout.get("benchmark_source_hashes"),
        "benchmark_source_files": holdout.get("benchmark_source_files"),
        "comparator_metrics": holdout.get("comparator_metrics"),
        "validation_sha256": validation_sha256,
        "generated_by": GENERATOR,
        "formal_validation_or_holdout_accessed": False,
    }


def _existing_result(db, version: StrategyVersion) -> dict[str, object] | None:
    account = db.scalar(
        select(StockPaperAccount).where(
            StockPaperAccount.strategy_version_id == version.id
        )
    )
    if version.status != "paper_operational_validation" or account is None:
        return None
    return {
        "strategy_version_id": version.id,
        "account_id": account.id,
        "status": version.status,
        "trial_start": account.trial_start.isoformat(),
        "trial_end": account.trial_end.isoformat(),
        "formal_validation_or_holdout_accessed": False,
        "investment_approval_eligible": False,
        "validation_sha256": version.params.get("validation_sha256"),
        "idempotent": True,
    }


def run() -> dict[str, object]:
    root = Path(__file__).resolve().parents[3]
    git_sha, git_status_sha256 = _git_snapshot(root)
    db = SessionLocal()
    try:
        previous = db.scalar(
            select(StrategyVersion)
            .where(StrategyVersion.name == PREVIOUS_SHADOW_NAME)
            .order_by(StrategyVersion.id.desc())
            .limit(1)
        )
        if previous is not None and previous.status != "retired":
            contaminated_runs = list(
                db.scalars(
                    select(StockPaperRun.id)
                    .join(
                        StockPaperAccount,
                        StockPaperRun.account_id == StockPaperAccount.id,
                    )
                    .where(
                        StockPaperAccount.strategy_version_id == previous.id,
                    )
                ).all()
            )
            strategy_lifecycle.transition(
                db,
                previous.id,
                "retired",
                evidence={
                    "runtime_source_gate_missing": True,
                    "recorded_git_sha": dict(previous.params or {}).get("git_sha"),
                    "replacement_git_sha": git_sha,
                    "contaminated_run_ids": contaminated_runs,
                    "formal_validation_or_holdout_accessed": False,
                },
                actor="system:strategy-v16-operational-shadow",
                reason=(
                    "版本15缺少每次推进账户前的运行时源码冻结校验；首日记录"
                    "保留作工程证据，但长期观察迁移到带失败关闭门禁的新版本"
                ),
            )
            previous_accounts = db.scalars(
                select(StockPaperAccount).where(
                    StockPaperAccount.strategy_version_id == previous.id
                )
            ).all()
            for account in previous_accounts:
                account.status = "superseded_missing_runtime_source_gate"
            db.add(
                AuditLog(
                    actor="system:strategy-v16-operational-shadow",
                    action="operational_shadow_invalidated",
                    resource_type="strategy_version",
                    resource_id=str(previous.id),
                    detail={
                        "recorded_git_sha": dict(previous.params or {}).get("git_sha"),
                        "replacement_git_sha": git_sha,
                        "contaminated_run_ids": contaminated_runs,
                        "account_ids": [item.id for item in previous_accounts],
                        "formal_validation_or_holdout_accessed": False,
                    },
                    created_at=datetime.now(UTC),
                )
            )
            db.commit()
        existing = db.scalar(
            select(StrategyVersion)
            .where(StrategyVersion.name == stock_paper.OPERATIONAL_SHADOW_NAME)
            .order_by(StrategyVersion.id.desc())
            .limit(1)
        )
        if existing is not None:
            result = _existing_result(db, existing)
            if result is not None:
                return result
            raise RuntimeError(
                f"版本16已有未完成记录 id={existing.id} status={existing.status}"
            )

        source = db.get(StrategyVersion, SOURCE_VERSION_ID)
        if source is None:
            raise RuntimeError("版本11研究记录不存在")
        source_preflight = dict(
            dict(source.params or {}).get("training_preflight") or {}
        )
        weights = {
            str(key): float(value)
            for key, value in dict(
                source_preflight.get("frozen_factor_weights") or {}
            ).items()
        }
        if set(weights) != {"quality", "value", "momentum", "trend", "lowvol"}:
            raise RuntimeError("版本11缺少完整冻结因子权重")

        readiness = stock_paper.get_readiness(db)
        if not readiness.ready or not readiness.latest_data_date:
            raise RuntimeError(
                "当前运行 readiness 未通过：" + "；".join(readiness.blockers)
            )
        data_date = date.fromisoformat(readiness.latest_data_date)
        candidates = stock_paper._ready_candidate_codes(db, data_date)
        if len(candidates) != stock_paper.EXPECTED_UNIVERSE_COUNT:
            raise RuntimeError(f"运行影子候选池不是800只：{len(candidates)}")
        candidate_sha256 = hashlib.sha256(
            json.dumps(candidates, ensure_ascii=False, separators=(",", ":")).encode()
        ).hexdigest()
        mandate = strategy_mandate.operational_validation_mandate(
            strategy_name=stock_paper.OPERATIONAL_SHADOW_NAME,
            initial_capital=stock_paper.INITIAL_CAPITAL,
            rebalance_days=20,
            top_n=stock_paper.TOP_N,
        )
        params = {
            "asset": "cn_stock",
            "model_version": stock_paper.MODEL_VERSION,
            "purpose": "operational_shadow_only",
            "validation_scope": "operational_only",
            "investment_approval_eligible": False,
            "shadow_of_strategy_version_id": source.id,
            "source_training_preflight_generated_at": source_preflight.get(
                "generated_at"
            ),
            "formal_validation_or_holdout_accessed": False,
            "git_sha": git_sha,
            "git_worktree_clean": True,
            "git_status_sha256": git_status_sha256,
            "runtime": {"python": sys.version, "platform": platform.platform()},
            "candidate_count": len(candidates),
            "candidate_sha256": candidate_sha256,
            "data_as_of": data_date.isoformat(),
            "frozen_adaptive_factor_weights": weights,
            "operational_replay_scope": {
                "folds": [
                    [start.isoformat(), end.isoformat()] for start, end in REPLAY_FOLDS
                ],
                "validation": [
                    VALIDATION_START.isoformat(),
                    VALIDATION_END.isoformat(),
                ],
                "allowed_use": "operational_chain_only",
                "formal_validation_or_holdout_accessed": False,
            },
            "methodology": (
                str(source.params.get("methodology") or "")
                + " 本版本是版本11的零参数改动运行影子，只验证数据、信号、"
                "成交、账本、对账和调度；收益不得用于投资有效性或调参。"
            ),
        }
        version = StrategyVersion(
            name=stock_paper.OPERATIONAL_SHADOW_NAME,
            initial_capital=stock_paper.INITIAL_CAPITAL,
            rebalance_interval=20,
            fee_rate=source.fee_rate,
            top_n=stock_paper.TOP_N,
            params=params,
            mandate=mandate,
            mandate_sha256=strategy_mandate.mandate_sha256(mandate),
            status="research",
        )
        db.add(version)
        db.commit()
        db.refresh(version)

        repository = load_repository(db)
        if repository is None:
            raise RuntimeError("股票研究仓储不可用")
        folds: list[dict[str, object]] = []
        for index, (start, end) in enumerate(REPLAY_FOLDS, start=1):
            outcome = stock_backtest.run_backtest(
                config=_config(
                    start=start,
                    end=end,
                    version_id=version.id,
                    factor_weights=weights,
                ),
                repository=repository,
            )
            stock_validation._assert_strategy_activity(
                outcome, stage=f"版本16运行回放折{index}"
            )
            folds.append(stock_validation._metrics(outcome))
        validation_outcome = stock_backtest.run_backtest(
            config=_config(
                start=VALIDATION_START,
                end=VALIDATION_END,
                version_id=version.id,
                factor_weights=weights,
            ),
            repository=repository,
        )
        stock_validation._assert_strategy_activity(
            validation_outcome, stage="版本16运行回放验证段"
        )
        validation = {
            "kind": "strategy_v16_operational_development_replay",
            "folds": folds,
            "validation": stock_validation._metrics(validation_outcome),
            "holdout_evaluations": 0,
            "formal_validation_or_holdout_accessed": False,
            "investment_effectiveness_use_forbidden": True,
        }
        validation_sha256 = stock_validation.validation_sha256(validation)
        evidence = _evidence(validation, validation_sha256=validation_sha256)
        params = dict(version.params)
        params["validation"] = validation
        params["validation_sha256"] = validation_sha256
        params["operational_validation_evidence"] = evidence
        version.params = params
        db.commit()

        version = strategy_lifecycle.transition(
            db,
            version.id,
            "operational_validated",
            evidence=evidence,
            actor="system:strategy-v16-operational-shadow",
            reason=("三个开发折与一个开发验证段的运行链路回放通过；未访问正式留出集"),
        )
        version = strategy_lifecycle.transition(
            db,
            version.id,
            "paper_operational_validation",
            evidence={
                "experiment_snapshot_complete": True,
                "validation_sha256": validation_sha256,
            },
            actor="system:strategy-v16-operational-shadow",
            reason=("代码、候选池、冻结权重与运行回放证据已冻结，启动两个月运行影子"),
        )
        account, _ = stock_paper._ensure_account(db, data_date)
        db.add(
            AuditLog(
                actor="system:strategy-v16-operational-shadow",
                action="operational_shadow_started",
                resource_type="strategy_version",
                resource_id=str(version.id),
                detail={
                    "source_strategy_version_id": source.id,
                    "account_id": account.id,
                    "trial_start": account.trial_start.isoformat(),
                    "trial_end": account.trial_end.isoformat(),
                    "formal_validation_or_holdout_accessed": False,
                    "investment_approval_eligible": False,
                    "validation_sha256": validation_sha256,
                },
                created_at=datetime.now(UTC),
            )
        )
        db.commit()
        return {
            "strategy_version_id": version.id,
            "account_id": account.id,
            "status": version.status,
            "trial_start": account.trial_start.isoformat(),
            "trial_end": account.trial_end.isoformat(),
            "formal_validation_or_holdout_accessed": False,
            "investment_approval_eligible": False,
            "validation_sha256": validation_sha256,
            "idempotent": False,
        }
    finally:
        db.close()


if __name__ == "__main__":
    print(json.dumps(run(), ensure_ascii=False, sort_keys=True))
