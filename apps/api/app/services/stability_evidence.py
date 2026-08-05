"""年度、事前市场状态、行业与市值组的 Alpha 稳定性硬门禁。"""

from __future__ import annotations

import math
from collections import defaultdict
from datetime import date
from statistics import fmean

from app.services.quant_stats import rank_ic


def _compound(values: list[float]) -> float:
    return math.prod(1.0 + value for value in values) - 1.0


def _ex_ante_regimes(benchmark_returns: list[float]) -> list[str]:
    """只用 t 日之前最多 60 日的基准收益标注 t 日，绝不看未来。"""
    labels: list[str] = []
    for index in range(len(benchmark_returns)):
        history = benchmark_returns[max(0, index - 60) : index]
        if len(history) < 20:
            labels.append("warmup")
            continue
        trend = _compound(history)
        mean = fmean(history)
        volatility = math.sqrt(
            sum((value - mean) ** 2 for value in history)
            / max(len(history) - 1, 1)
        ) * math.sqrt(252.0)
        if volatility >= 0.30:
            labels.append("high_volatility")
        elif trend >= 0.05:
            labels.append("bull")
        elif trend <= -0.05:
            labels.append("bear")
        else:
            labels.append("sideways")
    return labels


def stability_evidence(
    calendar: list[date],
    strategy_returns: list[float],
    benchmark_curve: list[float],
    scores_by_date: list[tuple[date, dict[str, float]]],
    forward_returns: list[tuple[date, dict[str, float]]],
    groups_by_date: list[
        tuple[date, dict[str, tuple[str, str]]]
    ],
    *,
    minimum_worst_year_excess_return: float = -0.05,
    minimum_worst_regime_excess_return: float = -0.05,
    maximum_single_group_contribution: float = 0.50,
    minimum_after_best_year_removed: float = 0.0,
) -> dict[str, object]:
    benchmark_returns = [
        benchmark_curve[index] / benchmark_curve[index - 1] - 1.0
        for index in range(1, len(benchmark_curve))
        if benchmark_curve[index - 1] > 0
    ]
    count = min(len(strategy_returns), len(benchmark_returns), max(len(calendar) - 1, 0))
    days = calendar[1 : count + 1]
    active = [
        strategy_returns[index] - benchmark_returns[index]
        for index in range(count)
    ]
    annual_series: dict[str, list[float]] = defaultdict(list)
    for day, value in zip(days, active, strict=True):
        annual_series[str(day.year)].append(value)
    annual = {
        year: {
            "days": len(values),
            "net_excess_return": _compound(values),
        }
        for year, values in sorted(annual_series.items())
    }

    regimes = _ex_ante_regimes(benchmark_returns[:count])
    regime_series: dict[str, list[float]] = defaultdict(list)
    for label, value in zip(regimes, active, strict=True):
        regime_series[label].append(value)
    regime_metrics = {
        label: {
            "days": len(values),
            "net_excess_return": _compound(values),
            "label_kind": "ex_ante_trailing_60d",
        }
        for label, values in sorted(regime_series.items())
    }

    forwards = dict(forward_returns)
    groups = dict(groups_by_date)
    grouped_ic: dict[str, list[float]] = defaultdict(list)
    grouped_alpha: dict[str, list[float]] = defaultdict(list)
    for signal_date, scores in scores_by_date:
        period_returns = forwards.get(signal_date, {})
        period_groups = groups.get(signal_date, {})
        available = [
            code for code in scores if code in period_returns and code in period_groups
        ]
        if not available:
            continue
        universe_mean = fmean(period_returns[code] for code in available)
        for kind, position in (("industry", 0), ("size", 1)):
            labels = sorted({period_groups[code][position] for code in available})
            for label in labels:
                selected = [
                    code
                    for code in available
                    if period_groups[code][position] == label
                ]
                key = f"{kind}:{label}"
                if len(selected) >= 3:
                    value = rank_ic(
                        [scores[code] for code in selected],
                        [period_returns[code] for code in selected],
                    )
                    if value is not None:
                        grouped_ic[key].append(value)
                top_count = max(1, math.ceil(len(selected) * 0.20))
                top = sorted(
                    selected, key=lambda code: (scores[code], code), reverse=True
                )[:top_count]
                grouped_alpha[key].append(
                    fmean(period_returns[code] for code in top) - universe_mean
                )
    group_metrics = {
        key: {
            "periods": len(grouped_alpha[key]),
            "net_excess_return": _compound(grouped_alpha[key]),
            "mean_rank_ic": (
                fmean(grouped_ic[key]) if grouped_ic.get(key) else None
            ),
        }
        for key in sorted(grouped_alpha)
    }
    contributions = {
        key: float(metric["net_excess_return"])
        for key, metric in group_metrics.items()
    }
    denominator = sum(abs(value) for value in contributions.values())
    maximum_contribution = (
        max((abs(value) / denominator for value in contributions.values()), default=0.0)
        if denominator > 0
        else None
    )
    best_year = (
        max(annual, key=lambda year: float(annual[year]["net_excess_return"]))
        if annual
        else None
    )
    without_best = [
        value
        for day, value in zip(days, active, strict=True)
        if best_year is None or str(day.year) != best_year
    ]
    after_best_removed = _compound(without_best) if without_best else None
    worst_year = min(
        (float(item["net_excess_return"]) for item in annual.values()),
        default=None,
    )
    material_regimes = [
        float(item["net_excess_return"])
        for key, item in regime_metrics.items()
        if key != "warmup" and int(item["days"]) >= 20
    ]
    worst_regime = min(material_regimes, default=None)
    failures: list[str] = []
    if worst_year is None or worst_year < minimum_worst_year_excess_return:
        failures.append("最差年度超额收益低于门槛")
    if worst_regime is None or worst_regime < minimum_worst_regime_excess_return:
        failures.append("最差事前行情状态超额收益低于门槛")
    if (
        maximum_contribution is None
        or maximum_contribution > maximum_single_group_contribution
    ):
        failures.append("Alpha过度集中于单一行业/市值组")
    if (
        after_best_removed is None
        or after_best_removed <= minimum_after_best_year_removed
    ):
        failures.append("删除最佳年度后不再满足最低超额收益标准")
    return {
        "regime_definition": {
            "kind": "ex_ante",
            "inputs": "benchmark returns strictly before labeled day",
            "lookback_trading_days": 60,
        },
        "annual": annual,
        "regimes": regime_metrics,
        "groups": group_metrics,
        "best_year": best_year,
        "excess_return_after_best_year_removed": after_best_removed,
        "worst_year_excess_return": worst_year,
        "worst_regime_excess_return": worst_regime,
        "max_single_group_alpha_contribution": maximum_contribution,
        "failures": failures,
        "passed": not failures,
        "status": "passed" if not failures else "failed",
    }
