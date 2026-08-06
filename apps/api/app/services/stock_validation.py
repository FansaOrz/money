"""股票策略 purged walk-forward、验证集与一次性留出集评估。"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import replace
from datetime import date
from statistics import fmean

from app.services import quant_stats
from app.services.stock_backtest import (
    BacktestConfig,
    BacktestError,
    BacktestOutcome,
    run_backtest,
)
from app.services.stock_repository import StockRepository


def _curve_sha256(
    calendar: list[date],
    values: list[float],
) -> str:
    """对与交易日历严格对齐的净值曲线生成稳定摘要。"""
    if len(calendar) != len(values):
        raise BacktestError("曲线与交易日历长度不一致，拒绝生成验证证据")
    payload = [
        [day.isoformat(), format(float(value), ".17g")]
        for day, value in zip(calendar, values, strict=True)
    ]
    canonical = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def validation_sha256(validation: dict[str, object]) -> str:
    """生成正式验证结果的规范化摘要，供冻结与晋级门禁共同使用。"""
    canonical = json.dumps(
        validation,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


def _metrics(outcome: BacktestOutcome) -> dict[str, object]:
    if len(outcome.calendar) != len(outcome.benchmark):
        raise BacktestError("基准曲线与策略交易日历未严格对齐")
    total_return = (
        outcome.final_value / outcome.equity[0] - 1.0
        if outcome.equity and outcome.equity[0] > 0
        else None
    )

    def sample_std(values: list[float]) -> float | None:
        if len(values) < 2:
            return None
        mean = fmean(values)
        return math.sqrt(
            sum((value - mean) ** 2 for value in values) / (len(values) - 1)
        )

    def relative(curve: list[float]) -> dict[str, float | None]:
        if len(curve) != len(outcome.calendar):
            raise BacktestError("比较基准曲线与策略交易日历未严格对齐")
        benchmark_return = (
            curve[-1] / curve[0] - 1.0 if len(curve) >= 2 and curve[0] > 0 else None
        )
        benchmark_daily = [
            curve[index] / curve[index - 1] - 1.0
            for index in range(1, len(curve))
            if curve[index - 1] > 0
        ]
        aligned = list(zip(outcome.daily_returns, benchmark_daily, strict=False))
        active = [strategy - benchmark for strategy, benchmark in aligned]
        active_std = sample_std(active)
        tracking_error = (
            active_std * math.sqrt(252.0) if active_std is not None else None
        )
        information_ratio = (
            fmean(active) / active_std * math.sqrt(252.0)
            if active and active_std not in (None, 0.0)
            else None
        )
        beta = alpha = None
        if len(aligned) >= 2:
            strategy_mean = fmean(item[0] for item in aligned)
            benchmark_mean = fmean(item[1] for item in aligned)
            benchmark_variance = sum(
                (item[1] - benchmark_mean) ** 2 for item in aligned
            )
            if benchmark_variance > 0:
                beta = (
                    sum(
                        (strategy - strategy_mean) * (benchmark - benchmark_mean)
                        for strategy, benchmark in aligned
                    )
                    / benchmark_variance
                )
                alpha = (strategy_mean - beta * benchmark_mean) * 252.0

        def capture(positive: bool) -> float | None:
            selected = [
                (strategy, benchmark)
                for strategy, benchmark in aligned
                if (benchmark > 0 if positive else benchmark < 0)
            ]
            if not selected:
                return None
            strategy_period = math.prod(1.0 + item[0] for item in selected) - 1.0
            benchmark_period = math.prod(1.0 + item[1] for item in selected) - 1.0
            return (
                strategy_period / benchmark_period
                if abs(benchmark_period) > 1e-12
                else None
            )

        return {
            "benchmark_return": benchmark_return,
            "net_excess_return": (
                total_return - benchmark_return
                if total_return is not None and benchmark_return is not None
                else None
            ),
            "tracking_error": tracking_error,
            "active_sharpe": information_ratio,
            "information_ratio": information_ratio,
            "annualized_alpha": alpha,
            "beta": beta,
            "up_capture": capture(True),
            "down_capture": capture(False),
            "curve_sha256": _curve_sha256(outcome.calendar, curve),
        }

    primary = relative(outcome.benchmark)
    comparator_metrics = {
        kind: {
            **relative(curve),
            "metadata": outcome.benchmark_metadata_by_kind.get(kind, {}),
        }
        for kind, curve in outcome.benchmarks.items()
    }

    return {
        "total_return": total_return,
        "benchmark_return": primary["benchmark_return"],
        "net_excess_return": primary["net_excess_return"],
        "benchmark_kind": outcome.benchmark_kind,
        "benchmark_code": outcome.benchmark_metadata.get("code")
        or (
            outcome.benchmark_kind.split(":", 1)[1]
            if ":" in outcome.benchmark_kind
            else outcome.benchmark_kind
        ),
        "benchmark_name": outcome.benchmark_metadata.get("name"),
        "benchmark_return_kind": outcome.benchmark_metadata.get("return_kind"),
        "benchmark_source": outcome.benchmark_metadata.get("source"),
        "benchmark_source_files": outcome.benchmark_metadata.get("source_files"),
        "benchmark_source_hashes": outcome.benchmark_metadata.get("source_hashes"),
        "benchmark_source_rows": outcome.benchmark_metadata.get("source_rows"),
        "benchmark_source_first_date": outcome.benchmark_metadata.get(
            "source_first_date"
        ),
        "benchmark_source_last_date": outcome.benchmark_metadata.get(
            "source_last_date"
        ),
        "benchmark_curve_sha256": primary["curve_sha256"],
        "benchmark_start_date": (
            outcome.calendar[0].isoformat() if outcome.calendar else None
        ),
        "benchmark_end_date": (
            outcome.calendar[-1].isoformat() if outcome.calendar else None
        ),
        "benchmark_curve_points": len(outcome.benchmark),
        "strategy_curve_sha256": _curve_sha256(outcome.calendar, outcome.equity),
        "sharpe": quant_stats.sharpe_ratio(outcome.daily_returns),
        "active_sharpe": primary["active_sharpe"],
        "information_ratio": primary["information_ratio"],
        "tracking_error": primary["tracking_error"],
        "annualized_alpha": primary["annualized_alpha"],
        "beta": primary["beta"],
        "up_capture": primary["up_capture"],
        "down_capture": primary["down_capture"],
        "comparator_metrics": comparator_metrics,
        "max_drawdown": quant_stats.max_drawdown(outcome.equity),
        "turnover": outcome.avg_turnover,
        "trade_count": sum(len(rebalance.fills) for rebalance in outcome.rebalances),
        "non_empty_target_count": sum(
            bool(rebalance.target) for rebalance in outcome.rebalances
        ),
        "average_target_invested_weight": (
            sum(1.0 - rebalance.cash_weight for rebalance in outcome.rebalances)
            / len(outcome.rebalances)
            if outcome.rebalances
            else 0.0
        ),
        "fees": outcome.total_fees,
        "trading_days": len(outcome.calendar),
        "rebalance_count": len(outcome.rebalances),
        "minimum_data_coverage": outcome.minimum_historical_coverage,
        "execution_limit_data_coverage": (outcome.execution_limit_data_coverage),
    }


def _assert_strategy_activity(
    outcome: BacktestOutcome,
    *,
    stage: str,
) -> None:
    """正式验证不得把现金利息曲线误当作有效策略结果。"""
    non_empty_targets = [
        rebalance for rebalance in outcome.rebalances if rebalance.target
    ]
    trade_count = sum(len(rebalance.fills) for rebalance in outcome.rebalances)
    if non_empty_targets and trade_count > 0 and outcome.avg_turnover > 0:
        return
    diagnostic = next(
        (
            {
                "signal_date": rebalance.signal_date.isoformat(),
                "selection_funnel": rebalance.diagnostics.get("selection_funnel"),
                "warnings": rebalance.warnings[:5],
            }
            for rebalance in outcome.rebalances
            if not rebalance.target or rebalance.warnings
        ),
        None,
    )
    raise BacktestError(
        f"{stage}未产生可验证的真实策略活动："
        f"非空目标期数={len(non_empty_targets)}、成交数={trade_count}、"
        f"平均换手={outcome.avg_turnover:.6f}；"
        f"首个诊断={json.dumps(diagnostic, ensure_ascii=False, default=str)}"
    )


def _stability(outcome: BacktestOutcome) -> dict[str, object]:
    annual: dict[str, list[float]] = {}
    for day, value in zip(outcome.calendar[1:], outcome.daily_returns, strict=False):
        annual.setdefault(str(day.year), []).append(value)
    annual_metrics = {
        year: {
            "return": math.prod(1.0 + value for value in values) - 1.0,
            "sharpe": quant_stats.sharpe_ratio(values),
        }
        for year, values in annual.items()
    }
    benchmark_returns = [
        outcome.benchmark[index] / outcome.benchmark[index - 1] - 1.0
        for index in range(1, len(outcome.benchmark))
        if outcome.benchmark[index - 1] > 0
    ]
    regimes = {
        "up_market": [
            value
            for value, benchmark in zip(
                outcome.daily_returns, benchmark_returns, strict=False
            )
            if benchmark >= 0
        ],
        "down_market": [
            value
            for value, benchmark in zip(
                outcome.daily_returns, benchmark_returns, strict=False
            )
            if benchmark < 0
        ],
    }
    regime_metrics = {
        name: {
            "days": len(values),
            "mean_daily_return": sum(values) / len(values) if values else None,
        }
        for name, values in regimes.items()
    }
    forwards = dict(outcome.forward_returns)
    groups = dict(outcome.groups_by_date)
    group_ics: dict[str, list[float]] = {}
    for day, scores in outcome.scores_by_date:
        returns = forwards.get(day, {})
        for code, (industry, size) in groups.get(day, {}).items():
            if code not in scores or code not in returns:
                continue
            for label in (f"industry:{industry}", f"size:{size}"):
                group_ics.setdefault(label, [])
        for label in list(group_ics):
            prefix, group = label.split(":", 1)
            selected = [
                code
                for code, values in groups.get(day, {}).items()
                if (values[0] if prefix == "industry" else values[1]) == group
                and code in scores
                and code in returns
            ]
            if len(selected) >= 3:
                ic = quant_stats.rank_ic(
                    [scores[code] for code in selected],
                    [returns[code] for code in selected],
                )
                if ic is not None:
                    group_ics[label].append(ic)
    return {
        "annual": annual_metrics,
        "regimes": regime_metrics,
        "group_rank_ic": {
            group: sum(values) / len(values)
            for group, values in group_ics.items()
            if values
        },
    }


def _split_days(days: list[date]) -> tuple[list[date], list[date], list[date]]:
    if len(days) < 252 * 3:
        raise BacktestError("走步验证至少需要约3年（756个交易日）")
    train_end = max(int(len(days) * 0.60), 1)
    validation_end = max(int(len(days) * 0.80), train_end + 1)
    return days[:train_end], days[train_end:validation_end], days[validation_end:]


def planned_holdout_interval(
    repository: StockRepository,
    start: date,
    end: date,
) -> tuple[date, date]:
    days = [
        day for day in repository.trade_calendar(start, end).days if start <= day <= end
    ]
    _, _, holdout = _split_days(days)
    return holdout[0], holdout[-1]


def run_stock_walk_forward(
    repository: StockRepository,
    base: BacktestConfig,
    top_n_grid: list[int],
    max_stock_weight_grid: list[float],
    embargo_days: int = 21,
    robustness_scenario_results: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    """仅用训练段的 purged 折选择参数，验证/留出各评估一次。"""
    label_holding_days = 21
    from app.services.nested_time_validation import validate_label_gap

    validate_label_gap(
        label_holding_days=label_holding_days,
        purge_days=embargo_days,
        embargo_days=embargo_days,
    )
    days = [
        day
        for day in repository.trade_calendar(base.start, base.end).days
        if base.start <= day <= base.end
    ]
    train, validation, holdout = _split_days(days)
    if len(train) < 504 or len(validation) < 63 or len(holdout) < 63:
        raise BacktestError("训练/验证/留出切分后样本不足")

    initial_train = max(252, len(train) // 2)
    test_window = max(
        63,
        (len(train) - initial_train - 3 * embargo_days) // 3,
    )
    folds: list[tuple[date, date]] = []
    cursor = initial_train
    while cursor + embargo_days < len(train):
        start_index = cursor + embargo_days
        end_index = min(start_index + test_window - 1, len(train) - 1)
        if end_index <= start_index:
            break
        folds.append((train[start_index], train[end_index]))
        cursor = end_index + 1
    if len(folds) < 3:
        raise BacktestError("训练段无法形成至少3个带 embargo 的样本外走步折")

    trials: list[dict[str, object]] = []
    for top_n in sorted(set(top_n_grid)):
        for max_weight in sorted(set(max_stock_weight_grid)):
            fold_metrics: list[dict[str, float | int | None]] = []
            for fold_start, fold_end in folds:
                outcome = run_backtest(
                    config=replace(
                        base,
                        start=fold_start,
                        end=fold_end,
                        top_n=top_n,
                        max_stock_weight=max_weight,
                        initial_signal=False,
                    ),
                    repository=repository,
                )
                _assert_strategy_activity(
                    outcome,
                    stage=(
                        f"训练走步折 {fold_start.isoformat()}~{fold_end.isoformat()}"
                    ),
                )
                fold_metrics.append(_metrics(outcome))
            sharpes = [
                float(item["sharpe"])
                for item in fold_metrics
                if item["sharpe"] is not None and math.isfinite(float(item["sharpe"]))
            ]
            returns = [
                float(item["total_return"])
                for item in fold_metrics
                if item["total_return"] is not None
            ]
            score = (sum(sharpes) / len(sharpes) if sharpes else -10.0) + (
                sum(returns) / len(returns) if returns else -10.0
            )
            trials.append(
                {
                    "params": {
                        "top_n": top_n,
                        "max_stock_weight": max_weight,
                    },
                    "score": score,
                    "folds": fold_metrics,
                }
            )
    best = max(trials, key=lambda item: float(item["score"]))
    from app.services.backtest_overfitting import cscv_pbo

    pbo = cscv_pbo(
        {
            json.dumps(trial["params"], sort_keys=True): [
                float(fold.get("sharpe") or 0.0)
                + float(fold.get("total_return") or 0.0)
                for fold in trial["folds"]  # type: ignore[union-attr]
            ]
            for trial in trials
        }
    )
    best_params = dict(best["params"])  # type: ignore[arg-type]
    selected = replace(
        base,
        top_n=int(best_params["top_n"]),
        max_stock_weight=float(best_params["max_stock_weight"]),
        initial_signal=False,
    )
    validation_outcome = run_backtest(
        config=replace(selected, start=validation[0], end=validation[-1]),
        repository=repository,
    )
    _assert_strategy_activity(validation_outcome, stage="验证集")
    # 留出集在参数冻结后只调用一次。
    holdout_outcome = run_backtest(
        config=replace(selected, start=holdout[0], end=holdout[-1]),
        repository=repository,
    )
    _assert_strategy_activity(holdout_outcome, stage="留出集")
    validation_metrics = _metrics(validation_outcome)
    holdout_metrics = _metrics(holdout_outcome)
    from app.services.experiment_registry import (
        effective_attempt_count_from_series,
    )

    trial_score_series = [
        [
            float(fold.get("sharpe") or 0.0) + float(fold.get("total_return") or 0.0)
            for fold in trial["folds"]  # type: ignore[union-attr]
        ]
        for trial in trials
    ]
    effective_trials = effective_attempt_count_from_series(trial_score_series)
    psr = quant_stats.probabilistic_sharpe(holdout_outcome.daily_returns)
    dsr = quant_stats.deflated_sharpe(
        holdout_outcome.daily_returns,
        max(1, math.ceil(effective_trials)),
    )
    holdout_metrics.update(
        {
            "effective_trial_count": effective_trials,
            "return_skewness": psr.skew if psr else None,
            "return_excess_kurtosis": psr.kurtosis if psr else None,
            "probabilistic_sharpe_probability": (psr.probability if psr else None),
            "minimum_track_record_length": (
                psr.minimum_track_record_length if psr else None
            ),
            "deflated_sharpe_probability": dsr.dsr if dsr else None,
            "expected_max_sharpe_under_trials": (dsr.expected_max_sr if dsr else None),
        }
    )
    from app.services.quintile_evidence import quintile_evidence

    quintile = quintile_evidence(
        holdout_outcome.scores_by_date,
        holdout_outcome.forward_returns,
    )
    holdout_metrics.update(
        {
            "quintile_monotonicity": quintile.get("quintile_monotonicity"),
            "top_bottom_spread": quintile.get("top_bottom_spread"),
            "top_bottom_ci_lower": (
                quintile["top_bottom_bootstrap_95_ci"][0]
                if quintile.get("top_bottom_bootstrap_95_ci")
                else None
            ),
            "top_bottom_hit_rate": quintile.get("top_bottom_hit_rate"),
            "quintile_gate_status": quintile.get("status"),
        }
    )
    from app.services.active_alpha_evidence import active_alpha_evidence

    holdout_benchmark_returns = [
        holdout_outcome.benchmark[index] / holdout_outcome.benchmark[index - 1] - 1.0
        for index in range(1, len(holdout_outcome.benchmark))
        if holdout_outcome.benchmark[index - 1] > 0
    ]
    active_alpha = active_alpha_evidence(
        holdout_outcome.daily_returns,
        holdout_benchmark_returns,
    )
    active_ci = active_alpha.get("active_block_bootstrap_95_ci")
    regression_ci = active_alpha.get("regression_alpha_block_bootstrap_95_ci")
    holdout_metrics.update(
        {
            "active_return_newey_west_t": active_alpha.get("active_newey_west_t"),
            "active_return_ci_lower": active_ci[0] if active_ci else None,
            "regression_alpha_ci_lower": (
                regression_ci[0] * 252.0 if regression_ci else None
            ),
            "active_alpha_gate_status": active_alpha.get("status"),
        }
    )
    from app.services.stability_evidence import stability_evidence

    hard_stability = stability_evidence(
        holdout_outcome.calendar,
        holdout_outcome.daily_returns,
        holdout_outcome.benchmark,
        holdout_outcome.scores_by_date,
        holdout_outcome.forward_returns,
        holdout_outcome.groups_by_date,
    )
    holdout_metrics.update(
        {
            "worst_year_excess_return": hard_stability.get("worst_year_excess_return"),
            "worst_regime_excess_return": hard_stability.get(
                "worst_regime_excess_return"
            ),
            "max_single_period_alpha_contribution": hard_stability.get(
                "max_single_group_alpha_contribution"
            ),
            "best_year_removed_excess_return": hard_stability.get(
                "excess_return_after_best_year_removed"
            ),
            "stability_gate_status": hard_stability.get("status"),
        }
    )
    from app.services.ic_significance import factor_ic_significance

    trial_pvalues: dict[str, float] = {}
    for index, series in enumerate(trial_score_series):
        if len(series) < 2:
            trial_pvalues[f"trial-{index}"] = 1.0
            continue
        mean = fmean(series)
        std = math.sqrt(
            sum((value - mean) ** 2 for value in series) / (len(series) - 1)
        )
        statistic = mean / (std / math.sqrt(len(series))) if std > 0 else 0.0
        trial_pvalues[f"trial-{index}"] = 2.0 * (
            1.0 - 0.5 * (1.0 + math.erf(abs(statistic) / math.sqrt(2.0)))
        )
    ic_evidence = factor_ic_significance(
        holdout_outcome.factor_values_by_date,
        holdout_outcome.forward_returns,
        extra_attempt_pvalues=trial_pvalues,
    )
    composite_ic = dict(dict(ic_evidence.get("factors") or {}).get("composite") or {})
    ci = composite_ic.get("block_bootstrap_95_ci")
    holdout_metrics.update(
        {
            "rank_ic_mean": composite_ic.get("mean"),
            "rank_icir": composite_ic.get("icir"),
            "rank_ic_p_value": composite_ic.get("p_value"),
            "rank_ic_ci_lower": ci[0] if ci else None,
            "rank_ic_effective_observations": composite_ic.get(
                "effective_observations"
            ),
            "multiple_testing_fdr": composite_ic.get("fdr_q_value"),
            "alpha_evidence_status": ic_evidence.get("status"),
        }
    )
    observed_coverages = [
        float(metric.get("minimum_data_coverage") or 0.0)
        for trial in trials
        for metric in trial["folds"]  # type: ignore[union-attr]
    ] + [
        float(validation_metrics["minimum_data_coverage"] or 0.0),
        float(holdout_metrics["minimum_data_coverage"] or 0.0),
    ]
    from app.services.robustness_scenarios import evaluate_robustness

    robustness = evaluate_robustness(
        [
            {
                "dimension": "cost_1x",
                "case": "formal_holdout_baseline",
                "net_excess_return": holdout_metrics.get("net_excess_return"),
                "source": "run_backtest",
            },
            *(robustness_scenario_results or []),
        ]
    )
    holdout_metrics.update(
        {
            "robustness_passed": robustness.get("passed"),
            "robustness_neighbor_pass_rate": robustness.get("neighbor_pass_rate"),
            "cost_2x_excess_return": next(
                (
                    row.get("net_excess_return")
                    for row in robustness["scenarios"]
                    if row.get("dimension") == "cost_2x"
                ),
                None,
            ),
            "robustness_gate_status": robustness.get("status"),
        }
    )
    return {
        "methodology": {
            "name": (
                "nested_purged_parameter_selection"
                if len(trials) > 1
                else "rolling_out_of_sample_evaluation_no_parameter_selection"
            ),
            "selection_performed": len(trials) > 1,
            "selectable_parameters": ["top_n", "max_stock_weight"],
            "fixed_policies": [
                "universe_indices",
                "factor_definition",
                "execution_rules",
                "benchmark",
                "risk_limits",
            ],
            "outer_test_role": "final_generalization_evaluation_only",
            "inner_validation_role": "parameter_and_model_selection_only",
            "label_holding_days": label_holding_days,
            "purge_days": embargo_days,
            "embargo_days": embargo_days,
            "fold_local_fit": [
                "standardization",
                "missing_value_policy",
                "neutralization",
                "ic_weight_estimation",
            ],
        },
        "splits": {
            "train": [train[0].isoformat(), train[-1].isoformat()],
            "validation": [validation[0].isoformat(), validation[-1].isoformat()],
            "holdout": [holdout[0].isoformat(), holdout[-1].isoformat()],
        },
        "embargo_days": embargo_days,
        "folds": [
            [fold_start.isoformat(), fold_end.isoformat()]
            for fold_start, fold_end in folds
        ],
        "trials": trials,
        "cscv_pbo": pbo,
        "probability_backtest_overfitting": pbo.get("probability_backtest_overfitting"),
        "best_params": best_params,
        "validation": validation_metrics,
        "holdout": holdout_metrics,
        "ic_significance": ic_evidence,
        "quintile_evidence": quintile,
        "active_alpha_evidence": active_alpha,
        "hard_stability_evidence": hard_stability,
        "robustness_evidence": robustness,
        "validation_stability": _stability(validation_outcome),
        "holdout_stability": _stability(holdout_outcome),
        "factor_redundancy": (
            __import__(
                "app.services.factor_redundancy",
                fromlist=["diagnose_factor_redundancy"],
            ).diagnose_factor_redundancy(
                validation_outcome.factor_values_by_date,
                validation_outcome.forward_returns,
            )
        ),
        "adaptive_factor_weight_history": (validation_outcome.factor_weight_history),
        "frozen_adaptive_factor_weights": (
            dict(validation_outcome.factor_weight_history[-1]["weights"])
            if validation_outcome.factor_weight_history
            else None
        ),
        "linear_alpha_challenger": (
            __import__(
                "app.services.linear_alpha_challenger",
                fromlist=[
                    "rows_from_backtest",
                    "walk_forward_linear_challenger",
                ],
            ).walk_forward_linear_challenger(
                __import__(
                    "app.services.linear_alpha_challenger",
                    fromlist=["rows_from_backtest"],
                ).rows_from_backtest(validation_outcome)
            )
        ),
        "holdout_evaluations": 1,
        "minimum_data_coverage": min(observed_coverages),
        "warnings": [
            "留出集只评估一次；修改参数后必须创建新的实验版本",
            "还需按年份、行情状态、行业与市值分组复核稳定性",
        ],
    }
