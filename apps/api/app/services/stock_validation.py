"""股票策略 purged walk-forward、验证集与一次性留出集评估。"""

from __future__ import annotations

import math
from dataclasses import replace
from datetime import date

from app.services import quant_stats
from app.services.stock_backtest import (
    BacktestConfig,
    BacktestError,
    BacktestOutcome,
    run_backtest,
)
from app.services.stock_repository import StockRepository


def _metrics(outcome: BacktestOutcome) -> dict[str, float | int | None]:
    total_return = (
        outcome.final_value / outcome.equity[0] - 1.0
        if outcome.equity and outcome.equity[0] > 0
        else None
    )
    return {
        "total_return": total_return,
        "sharpe": quant_stats.sharpe_ratio(outcome.daily_returns),
        "max_drawdown": quant_stats.max_drawdown(outcome.equity),
        "turnover": outcome.avg_turnover,
        "fees": outcome.total_fees,
        "trading_days": len(outcome.calendar),
        "rebalance_count": len(outcome.rebalances),
        "minimum_data_coverage": outcome.minimum_historical_coverage,
    }


def _stability(outcome: BacktestOutcome) -> dict[str, object]:
    annual: dict[str, list[float]] = {}
    for day, value in zip(
        outcome.calendar[1:], outcome.daily_returns, strict=False
    ):
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


def run_stock_walk_forward(
    repository: StockRepository,
    base: BacktestConfig,
    top_n_grid: list[int],
    max_stock_weight_grid: list[float],
    embargo_days: int = 21,
) -> dict[str, object]:
    """仅用训练段的 purged 折选择参数，验证/留出各评估一次。"""
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
            score = (
                (sum(sharpes) / len(sharpes) if sharpes else -10.0)
                + (sum(returns) / len(returns) if returns else -10.0)
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
    # 留出集在参数冻结后只调用一次。
    holdout_outcome = run_backtest(
        config=replace(selected, start=holdout[0], end=holdout[-1]),
        repository=repository,
    )
    validation_metrics = _metrics(validation_outcome)
    holdout_metrics = _metrics(holdout_outcome)
    observed_coverages = [
        float(metric.get("minimum_data_coverage") or 0.0)
        for trial in trials
        for metric in trial["folds"]  # type: ignore[union-attr]
    ] + [
        float(validation_metrics["minimum_data_coverage"] or 0.0),
        float(holdout_metrics["minimum_data_coverage"] or 0.0),
    ]
    return {
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
        "best_params": best_params,
        "validation": validation_metrics,
        "holdout": holdout_metrics,
        "validation_stability": _stability(validation_outcome),
        "holdout_stability": _stability(holdout_outcome),
        "holdout_evaluations": 1,
        "minimum_data_coverage": min(observed_coverages),
        "warnings": [
            "留出集只评估一次；修改参数后必须创建新的实验版本",
            "还需按年份、行情状态、行业与市值分组复核稳定性",
        ],
    }
