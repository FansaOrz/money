"""数据漂移、模型漂移与市场状态变化的分离监控。"""

from __future__ import annotations

import math
from collections.abc import Sequence
from statistics import fmean


def population_stability_index(
    baseline: Sequence[float],
    current: Sequence[float],
    *,
    bins: int = 10,
) -> float | None:
    if len(baseline) < bins or len(current) < bins:
        return None
    ordered = sorted(float(value) for value in baseline)
    boundaries = [
        ordered[min(int(len(ordered) * index / bins), len(ordered) - 1)]
        for index in range(1, bins)
    ]

    def proportions(values: Sequence[float]) -> list[float]:
        counts = [0] * bins
        for value in values:
            bucket = sum(float(value) > boundary for boundary in boundaries)
            counts[bucket] += 1
        return [max(count / len(values), 1e-6) for count in counts]

    expected = proportions(baseline)
    actual = proportions(current)
    return sum(
        (right - left) * math.log(right / left)
        for left, right in zip(expected, actual, strict=True)
    )


def classify_drift(
    *,
    baseline_features: Sequence[float],
    current_features: Sequence[float],
    feature_coverage: float,
    rolling_ic: Sequence[float],
    return_change: float,
    turnover_change: float,
    cost_change: float,
    exposure_change: float,
    market_volatility_change: float,
) -> dict[str, object]:
    psi = population_stability_index(baseline_features, current_features)
    recent_ic = fmean(rolling_ic[-3:]) if rolling_ic else None
    data_drift = (
        feature_coverage < 0.90 or psi is None or psi >= 0.25
    )
    model_drift = recent_ic is not None and recent_ic < -0.02
    market_regime_change = abs(market_volatility_change) >= 0.50
    action = "warning"
    if data_drift or model_drift:
        action = "stop"
    elif (
        market_regime_change
        or cost_change > 0.50
        or abs(exposure_change) > 0.30
    ):
        action = "downweight"
    return {
        "psi": psi,
        "feature_coverage": feature_coverage,
        "recent_ic": recent_ic,
        "data_drift": data_drift,
        "model_drift": model_drift,
        "market_regime_change": market_regime_change,
        "return_change": return_change,
        "turnover_change": turnover_change,
        "cost_change": cost_change,
        "exposure_change": exposure_change,
        "action": action,
        "thresholds": {
            "warning_psi": 0.10,
            "stop_psi": 0.25,
            "minimum_coverage": 0.90,
            "negative_ic_stop": -0.02,
        },
    }
