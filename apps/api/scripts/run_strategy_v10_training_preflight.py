"""版本10训练期开发预检；严格不调用正式走步/留出入口。"""

from __future__ import annotations

import argparse
import json
from datetime import UTC, date, datetime

from sqlalchemy import select

from app.db.session import SessionLocal
from app.models import AuditLog, StrategyVersion
from app.services import (
    active_alpha_evidence,
    factor_redundancy,
    ic_significance,
    linear_alpha_challenger,
    quintile_evidence,
    robustness_scenarios,
    stock_backtest,
    stock_factors,
    stock_paper,
    stock_validation,
)
from app.services.stock_repository import load_repository

FIT_START = date(2020, 1, 2)
FIT_END = date(2021, 12, 31)
DEVELOPMENT_START = date(2022, 1, 4)
DEVELOPMENT_END = date(2022, 12, 29)


def _config(
    *,
    start: date,
    end: date,
    version_id: int,
    factor_weights: dict[str, float] | None = None,
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
        adaptive_ic_weights=factor_weights is None,
        factor_weights=factor_weights,
        cost=stock_paper.COST,
        strategy_name=stock_paper.STRATEGY_NAME,
        strategy_version_id=version_id,
        strict_file_manifest=False,
    )


def _benchmark_returns(outcome: stock_backtest.BacktestOutcome) -> list[float]:
    return [
        outcome.benchmark[index] / outcome.benchmark[index - 1] - 1.0
        for index in range(1, len(outcome.benchmark))
        if outcome.benchmark[index - 1] > 0
    ]


def _compact_challenger(report: dict[str, object]) -> dict[str, object]:
    return {
        key: value
        for key, value in report.items()
        if key not in {"predictions", "coefficient_history"}
    } | {
        "prediction_count": len(report.get("predictions", [])),
        "coefficient_periods": len(report.get("coefficient_history", [])),
    }


def run_preflight(*, include_robustness: bool) -> dict[str, object]:
    db = SessionLocal()
    try:
        version = db.scalar(
            select(StrategyVersion)
            .where(StrategyVersion.name == stock_paper.STRATEGY_NAME)
            .order_by(StrategyVersion.id.desc())
            .limit(1)
        )
        if version is None:
            version = stock_paper.ensure_research_strategy_version(db)
        if version.status != "research":
            raise RuntimeError(
                f"版本10当前状态为 {version.status}，训练预检只允许 research"
            )
        repository = load_repository(db)
        if repository is None:
            raise RuntimeError("股票研究仓储不可用")

        fit = stock_backtest.run_backtest(
            config=_config(
                start=FIT_START,
                end=FIT_END,
                version_id=version.id,
            ),
            repository=repository,
        )
        stock_validation._assert_strategy_activity(
            fit, stage="版本10训练拟合段"
        )
        frozen_weights = (
            dict(fit.factor_weight_history[-1]["weights"])
            if fit.factor_weight_history
            else dict(stock_factors.DEFAULT_FAMILY_WEIGHTS)
        )
        development_config = _config(
            start=DEVELOPMENT_START,
            end=DEVELOPMENT_END,
            version_id=version.id,
            factor_weights=frozen_weights,
        )
        development = stock_backtest.run_backtest(
            config=development_config,
            repository=repository,
        )
        stock_validation._assert_strategy_activity(
            development, stage="版本10开发验证段"
        )

        ic = ic_significance.factor_ic_significance(
            development.factor_values_by_date,
            development.forward_returns,
        )
        quintile = quintile_evidence.quintile_evidence(
            development.scores_by_date,
            development.forward_returns,
        )
        active = active_alpha_evidence.active_alpha_evidence(
            development.daily_returns,
            _benchmark_returns(development),
        )
        challenger_rows = linear_alpha_challenger.rows_from_backtest(fit)
        challenger_rows.extend(
            linear_alpha_challenger.rows_from_backtest(development)
        )
        challenger = linear_alpha_challenger.walk_forward_linear_challenger(
            challenger_rows,
            prediction_start_date=DEVELOPMENT_START,
        )
        redundancy = factor_redundancy.diagnose_factor_redundancy(
            development.factor_values_by_date,
            development.forward_returns,
        )
        robustness_rows: list[dict[str, object]] = []
        robustness: dict[str, object] = {
            "status": "not_run",
            "passed": False,
            "reason": "使用 --robustness 才执行完整训练期压力目录",
        }
        if include_robustness:
            robustness_rows = (
                robustness_scenarios.run_validation_robustness(
                    repository,
                    development_config,
                    development,
                )
            )
            robustness = robustness_scenarios.evaluate_robustness(
                robustness_rows
            )

        composite = dict(
            dict(ic.get("factors") or {}).get("composite") or {}
        )
        result = {
            "kind": "strategy_v10_training_only_preflight",
            "strategy_version_id": version.id,
            "generated_at": datetime.now(UTC).isoformat(),
            "data_policy": {
                "fit": [FIT_START.isoformat(), FIT_END.isoformat()],
                "development_validation": [
                    DEVELOPMENT_START.isoformat(),
                    DEVELOPMENT_END.isoformat(),
                ],
                "formal_validation_or_holdout_accessed": False,
                "allowed_use": "development_only",
            },
            "frozen_factor_weights": frozen_weights,
            "factor_weight_history": fit.factor_weight_history,
            "fit_metrics": stock_validation._metrics(fit),
            "development_metrics": stock_validation._metrics(development),
            "composite_ic": composite,
            "ic_status": ic.get("status"),
            "tested_hypotheses": ic.get("tested_hypotheses"),
            "quintile": {
                key: value
                for key, value in quintile.items()
                if key != "periods"
            },
            "active_alpha": active,
            "linear_challenger": _compact_challenger(challenger),
            "factor_redundancy": {
                key: value
                for key, value in redundancy.items()
                if key != "periods"
            },
            "robustness": robustness,
            "robustness_scenarios": robustness_rows,
        }
        params = dict(version.params or {})
        params["training_preflight"] = result
        params["training_preflight_status"] = (
            "completed_with_robustness"
            if include_robustness
            else "completed_baseline"
        )
        version.params = params
        db.add(
            AuditLog(
                actor="system:strategy-v10-training-preflight",
                action="strategy_training_preflight",
                resource_type="strategy_version",
                resource_id=str(version.id),
                detail={
                    "fit": result["data_policy"]["fit"],
                    "development_validation": result["data_policy"][
                        "development_validation"
                    ],
                    "formal_validation_or_holdout_accessed": False,
                    "include_robustness": include_robustness,
                    "frozen_factor_weights": frozen_weights,
                    "development_net_excess_return": result[
                        "development_metrics"
                    ]["net_excess_return"],
                    "composite_ic_status": result["ic_status"],
                    "robustness_status": robustness.get("status"),
                },
                created_at=datetime.now(UTC),
            )
        )
        db.commit()
        return result
    finally:
        db.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--robustness",
        action="store_true",
        help="执行完整验证期扰动目录；耗时显著增加",
    )
    args = parser.parse_args()
    print(
        json.dumps(
            run_preflight(include_robustness=args.robustness),
            ensure_ascii=False,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
