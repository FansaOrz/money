"""参数、成本、容量、时点与数据扰动的统一稳健性证据。"""

from __future__ import annotations

import math
import hashlib
import json
from collections.abc import Iterable
from dataclasses import replace
from statistics import fmean
from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    from app.services.stock_backtest import (
        BacktestConfig,
        BacktestOutcome,
    )
    from app.services.stock_repository import StockRepository


REQUIRED_DIMENSIONS = {
    "top_n",
    "max_stock_weight",
    "rebalance_day",
    "factor_window",
    "factor_weight",
    "filter_threshold",
    "cost_1x",
    "cost_2x",
    "cost_3x",
    "capacity_down",
    "signal_delay",
    "delete_period",
    "delete_industry",
    "winsorization",
    "data_late",
}


def scenario_catalog() -> list[dict[str, object]]:
    """返回必须执行的扰动目录，供研究运行器和报告使用。"""
    return [
        {"dimension": "top_n", "values": ["-20%", "+20%"]},
        {"dimension": "max_stock_weight", "values": ["-20%", "+20%"]},
        {"dimension": "rebalance_day", "values": ["T-1", "T+1"]},
        {"dimension": "factor_window", "values": ["0.8x", "1.2x"]},
        {"dimension": "factor_weight", "values": ["family±20%"]},
        {"dimension": "filter_threshold", "values": ["0.8x", "1.2x"]},
        {"dimension": "cost_1x", "values": ["baseline"]},
        {"dimension": "cost_2x", "values": ["all-in cost 2x"]},
        {"dimension": "cost_3x", "values": ["all-in cost 3x"]},
        {"dimension": "capacity_down", "values": ["ADV participation 50%"]},
        {"dimension": "signal_delay", "values": ["+1 trading day"]},
        {"dimension": "delete_period", "values": ["leave-one-period-out"]},
        {"dimension": "delete_industry", "values": ["leave-one-industry-out"]},
        {"dimension": "winsorization", "values": ["0.5/99.5", "2/98"]},
        {"dimension": "data_late", "values": ["lag/drop newest observation"]},
    ]


def _compound(values: Iterable[float]) -> float:
    return math.prod(1.0 + value for value in values) - 1.0


def _outcome_metrics(outcome: "BacktestOutcome") -> dict[str, float]:
    strategy_return = (
        outcome.final_value / outcome.equity[0] - 1.0
        if outcome.equity and outcome.equity[0] > 0
        else float("nan")
    )
    benchmark_return = (
        outcome.benchmark[-1] / outcome.benchmark[0] - 1.0
        if outcome.benchmark and outcome.benchmark[0] > 0
        else float("nan")
    )
    return {
        "strategy_return": strategy_return,
        "benchmark_return": benchmark_return,
        "net_excess_return": strategy_return - benchmark_return,
    }


def _cost_multiplier(cost: object, multiplier: float) -> object:
    return replace(
        cost,
        commission_rate=cost.commission_rate * multiplier,
        min_commission=cost.min_commission * multiplier,
        stamp_tax_rate=cost.stamp_tax_rate * multiplier,
        slippage_rate=cost.slippage_rate * multiplier,
        market_impact_coefficient=(
            cost.market_impact_coefficient * multiplier
        ),
        volatility_slippage_coefficient=(
            cost.volatility_slippage_coefficient * multiplier
        ),
        max_total_slippage=max(
            cost.max_total_slippage,
            min(0.20, cost.max_total_slippage * multiplier),
        ),
    )


def _normalized_weight_tilt(
    weights: dict[str, float],
    family: str,
    multiplier: float,
) -> dict[str, float]:
    tilted = dict(weights)
    tilted[family] = max(0.0, tilted[family] * multiplier)
    total = sum(tilted.values())
    return {
        name: value / total
        for name, value in tilted.items()
    } if total > 0 else dict(weights)


def _case_hash(overrides: dict[str, object]) -> str:
    return hashlib.sha256(
        json.dumps(
            overrides,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()


def _delete_period_rows(
    baseline: "BacktestOutcome",
) -> list[dict[str, object]]:
    count = min(
        len(baseline.daily_returns),
        max(len(baseline.calendar) - 1, 0),
        max(len(baseline.benchmark) - 1, 0),
    )
    if count <= 0:
        return [
            {
                "dimension": "delete_period",
                "case": "insufficient_baseline_observations",
                "net_excess_return": None,
                "source": "validation_baseline_leave_block_out",
            }
        ]
    days = baseline.calendar[1 : count + 1]
    benchmark_returns = [
        baseline.benchmark[index] / baseline.benchmark[index - 1] - 1.0
        for index in range(1, count + 1)
    ]
    rows: list[dict[str, object]] = []
    for block in range(4):
        start = count * block // 4
        end = count * (block + 1) // 4
        kept = [index for index in range(count) if not start <= index < end]
        strategy_return = _compound(
            baseline.daily_returns[index] for index in kept
        )
        benchmark_return = _compound(
            benchmark_returns[index] for index in kept
        )
        rows.append(
            {
                "dimension": "delete_period",
                "case": f"leave_block_{block + 1}_out",
                "deleted_start": days[start].isoformat(),
                "deleted_end": days[end - 1].isoformat(),
                "strategy_return": strategy_return,
                "benchmark_return": benchmark_return,
                "net_excess_return": strategy_return - benchmark_return,
                "source": "validation_baseline_leave_block_out",
            }
        )
    return rows


def _material_industries(
    baseline: "BacktestOutcome",
    *,
    minimum_average_weight: float = 0.03,
) -> list[str]:
    groups = dict(baseline.groups_by_date)
    totals: dict[str, float] = {}
    periods = max(len(baseline.rebalances), 1)
    for rebalance in baseline.rebalances:
        labels = groups.get(rebalance.signal_date, {})
        for code, weight in rebalance.target.items():
            industry = labels.get(code, ("未知", "unknown"))[0]
            totals[industry] = totals.get(industry, 0.0) + float(weight)
    material = sorted(
        industry
        for industry, total in totals.items()
        if total / periods >= minimum_average_weight
    )
    if material:
        return material
    if totals:
        return [max(totals, key=totals.get)]
    return ["未知"]


def run_validation_robustness(
    repository: "StockRepository",
    base: "BacktestConfig",
    baseline: "BacktestOutcome",
    *,
    run_backtest_fn: Callable[..., "BacktestOutcome"] | None = None,
) -> list[dict[str, object]]:
    """仅在验证集运行预注册扰动，留出集始终只运行冻结基线一次。"""
    from app.services import stock_factors
    from app.services.stock_backtest import run_backtest

    runner = run_backtest_fn or run_backtest
    baseline_metrics = _outcome_metrics(baseline)
    rows: list[dict[str, object]] = [
        {
            "dimension": "cost_1x",
            "case": "validation_baseline",
            **baseline_metrics,
            "source": "validation_baseline",
        }
    ]

    def execute(
        dimension: str,
        case: str,
        config: "BacktestConfig",
        overrides: dict[str, object],
    ) -> None:
        try:
            outcome = runner(config=config, repository=repository)
            rows.append(
                {
                    "dimension": dimension,
                    "case": case,
                    **_outcome_metrics(outcome),
                    "trade_count": sum(
                        len(item.fills) for item in outcome.rebalances
                    ),
                    "turnover": outcome.avg_turnover,
                    "source": "validation_perturbation_run",
                    "overrides": overrides,
                    "case_sha256": _case_hash(overrides),
                }
            )
        except Exception as exc:  # noqa: BLE001 - 失败场景必须入证据而非中断目录
            rows.append(
                {
                    "dimension": dimension,
                    "case": case,
                    "net_excess_return": None,
                    "source": "validation_perturbation_run",
                    "overrides": overrides,
                    "case_sha256": _case_hash(overrides),
                    "error": str(exc),
                }
            )

    for multiplier in (0.8, 1.2):
        value = max(base.minimum_holdings, round(base.top_n * multiplier))
        execute(
            "top_n",
            f"{multiplier:.1f}x",
            replace(base, top_n=value),
            {"top_n": value},
        )
        weight = base.max_stock_weight * multiplier
        execute(
            "max_stock_weight",
            f"{multiplier:.1f}x",
            replace(base, max_stock_weight=weight),
            {"max_stock_weight": weight},
        )
        execute(
            "factor_window",
            f"{multiplier:.1f}x",
            replace(base, factor_window_scale=multiplier),
            {"factor_window_scale": multiplier},
        )
        amount = base.min_avg_amount * multiplier
        execute(
            "filter_threshold",
            f"{multiplier:.1f}x",
            replace(base, min_avg_amount=amount),
            {"min_avg_amount": amount},
        )
    for offset in (-1, 1):
        execute(
            "rebalance_day",
            f"T{offset:+d}",
            replace(base, rebalance_day_offset=offset),
            {"rebalance_day_offset": offset},
        )

    family_weights = (
        dict(baseline.factor_weight_history[-1]["weights"])
        if baseline.factor_weight_history
        else dict(stock_factors.DEFAULT_FAMILY_WEIGHTS)
    )
    for family in sorted(family_weights):
        for multiplier in (0.8, 1.2):
            weights = _normalized_weight_tilt(
                family_weights, family, multiplier
            )
            execute(
                "factor_weight",
                f"{family}_{multiplier:.1f}x",
                replace(
                    base,
                    adaptive_ic_weights=False,
                    factor_weights=weights,
                ),
                {"factor_weights": weights},
            )

    for multiplier in (2.0, 3.0):
        execute(
            f"cost_{int(multiplier)}x",
            f"all_in_{int(multiplier)}x",
            replace(
                base,
                cost=_cost_multiplier(base.cost, multiplier),
            ),
            {"all_in_cost_multiplier": multiplier},
        )
    participation = base.max_volume_participation * 0.5
    execute(
        "capacity_down",
        "adv_participation_50pct",
        replace(base, max_volume_participation=participation),
        {"max_volume_participation": participation},
    )
    delay = base.signal_execution_delay_days + 1
    execute(
        "signal_delay",
        "plus_one_trading_day",
        replace(base, signal_execution_delay_days=delay),
        {"signal_execution_delay_days": delay},
    )
    for quantiles in ((0.005, 0.995), (0.02, 0.98)):
        execute(
            "winsorization",
            f"{quantiles[0]:.3f}_{quantiles[1]:.3f}",
            replace(base, winsor_quantiles=quantiles),
            {"winsor_quantiles": list(quantiles)},
        )
    lag = base.data_lag_trading_days + 1
    execute(
        "data_late",
        "lag_latest_observation_one_day",
        replace(base, data_lag_trading_days=lag),
        {"data_lag_trading_days": lag},
    )
    for industry in _material_industries(baseline):
        execute(
            "delete_industry",
            f"exclude_{industry}",
            replace(base, excluded_industries=(industry,)),
            {"excluded_industries": [industry]},
        )
    rows.extend(_delete_period_rows(baseline))
    return rows


def evaluate_robustness(
    scenarios: Iterable[dict[str, object]],
    *,
    minimum_excess_return: float = 0.0,
    minimum_neighbor_pass_rate: float = 0.75,
) -> dict[str, object]:
    """汇总实际场景；目录缺项、邻域脆弱或保守成本失败均阻断晋级。"""
    rows = [dict(row) for row in scenarios]
    dimensions = {str(row.get("dimension")) for row in rows}
    missing = sorted(REQUIRED_DIMENSIONS - dimensions)
    for row in rows:
        value = row.get("net_excess_return")
        row["passed"] = bool(
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and math.isfinite(float(value))
            and float(value) > minimum_excess_return
        )
    neighbors = [
        row
        for row in rows
        if row.get("dimension")
        in {
            "top_n",
            "max_stock_weight",
            "rebalance_day",
            "factor_window",
            "factor_weight",
            "filter_threshold",
            "delete_period",
            "delete_industry",
            "winsorization",
            "data_late",
        }
    ]
    neighbor_pass_rate = (
        sum(row["passed"] is True for row in neighbors) / len(neighbors)
        if neighbors
        else 0.0
    )
    conservative = [
        row
        for row in rows
        if row.get("dimension")
        in {"cost_2x", "cost_3x", "capacity_down", "signal_delay"}
    ]
    failures: list[str] = []
    if missing:
        failures.append("缺少扰动维度：" + ",".join(missing))
    if neighbor_pass_rate < minimum_neighbor_pass_rate:
        failures.append("邻域参数/数据扰动通过率不足")
    if not conservative or any(row["passed"] is not True for row in conservative):
        failures.append("保守成本、容量或信号延迟场景存在失败")
    finite_values = [
        float(row["net_excess_return"])
        for row in rows
        if isinstance(row.get("net_excess_return"), (int, float))
        and not isinstance(row.get("net_excess_return"), bool)
    ]
    return {
        "catalog": scenario_catalog(),
        "scenarios": rows,
        "missing_dimensions": missing,
        "neighbor_pass_rate": neighbor_pass_rate,
        "worst_net_excess_return": min(finite_values) if finite_values else None,
        "mean_net_excess_return": fmean(finite_values) if finite_values else None,
        "failures": failures,
        "passed": not failures,
        "status": "passed" if not failures else "failed",
    }
