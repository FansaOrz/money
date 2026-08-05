"""逐期五档单调性、净头尾差、置信区间、胜率与换手成本。"""

from __future__ import annotations

import math
import random
from statistics import fmean


def _correlation(values: list[float]) -> float | None:
    if len(values) != 5:
        return None
    x = [1.0, 2.0, 3.0, 4.0, 5.0]
    x_mean = 3.0
    y_mean = fmean(values)
    numerator = sum(
        (left - x_mean) * (right - y_mean)
        for left, right in zip(x, values, strict=True)
    )
    denominator = math.sqrt(
        sum((left - x_mean) ** 2 for left in x)
        * sum((right - y_mean) ** 2 for right in values)
    )
    return numerator / denominator if denominator > 0 else None


def _bootstrap_ci(
    values: list[float],
    *,
    seed: int = 20260805,
    samples: int = 2000,
) -> tuple[float, float] | None:
    if len(values) < 3:
        return None
    generator = random.Random(seed)
    means = sorted(
        fmean(generator.choice(values) for _ in values)
        for _ in range(samples)
    )
    return (
        means[int(0.025 * (samples - 1))],
        means[int(0.975 * (samples - 1))],
    )


def quintile_evidence(
    scores_by_date: list[tuple[object, dict[str, float]]],
    forward_returns: list[tuple[object, dict[str, float]]],
    *,
    one_way_cost_rate: float = 0.001,
    minimum_economic_spread: float = 0.002,
    minimum_monotonic_correlation: float = 0.60,
) -> dict[str, object]:
    forwards = dict(forward_returns)
    previous_groups: list[set[str]] | None = None
    periods: list[dict[str, object]] = []
    spreads: list[float] = []
    monotonicities: list[float] = []
    for signal_date, scores in scores_by_date:
        returns = forwards.get(signal_date, {})
        ordered = sorted(
            (code for code in scores if code in returns),
            key=lambda code: (scores[code], code),
        )
        if len(ordered) < 10:
            continue
        groups = [
            set(
                ordered[
                    len(ordered) * bucket // 5 : len(ordered) * (bucket + 1) // 5
                ]
            )
            for bucket in range(5)
        ]
        gross = [
            fmean(returns[code] for code in group) if group else 0.0
            for group in groups
        ]
        turnover = [
            (
                1.0
                - len(group & previous_groups[index])
                / max(len(group), 1)
                if previous_groups is not None
                else 1.0
            )
            for index, group in enumerate(groups)
        ]
        net = [
            gross[index] - 2.0 * one_way_cost_rate * turnover[index]
            for index in range(5)
        ]
        correlation = _correlation(net)
        spread = net[-1] - net[0]
        spreads.append(spread)
        if correlation is not None:
            monotonicities.append(correlation)
        periods.append(
            {
                "signal_date": str(signal_date),
                "gross_returns": gross,
                "turnover": turnover,
                "net_returns": net,
                "monotonic_correlation": correlation,
                "top_bottom_net_spread": spread,
            }
        )
        previous_groups = groups
    mean_spread = fmean(spreads) if spreads else None
    mean_monotonicity = fmean(monotonicities) if monotonicities else None
    spread_ci = _bootstrap_ci(spreads)
    hit_rate = (
        sum(value > 0 for value in spreads) / len(spreads) if spreads else None
    )
    passed = bool(
        mean_spread is not None
        and mean_spread >= minimum_economic_spread
        and spread_ci is not None
        and spread_ci[0] > 0
        and mean_monotonicity is not None
        and mean_monotonicity >= minimum_monotonic_correlation
        and hit_rate is not None
        and hit_rate >= 0.60
    )
    reasons: list[str] = []
    if mean_monotonicity is None or mean_monotonicity < minimum_monotonic_correlation:
        reasons.append("五档净收益单调相关不足")
    if mean_spread is None or mean_spread < minimum_economic_spread:
        reasons.append("头尾净收益差缺乏经济意义")
    if spread_ci is None or spread_ci[0] <= 0:
        reasons.append("头尾差置信区间未严格高于零")
    if hit_rate is None or hit_rate < 0.60:
        reasons.append("逐期头尾胜率不足")
    return {
        "status": "passed" if passed else "failed",
        "passed": passed,
        "period_count": len(periods),
        "periods": periods,
        "quintile_monotonicity": mean_monotonicity,
        "top_bottom_spread": mean_spread,
        "top_bottom_bootstrap_95_ci": spread_ci,
        "top_bottom_hit_rate": hit_rate,
        "one_way_cost_rate": one_way_cost_rate,
        "minimum_economic_spread": minimum_economic_spread,
        "reasons": reasons,
    }
