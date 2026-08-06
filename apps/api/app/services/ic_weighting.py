"""仅使用已成熟训练标签的滚动 Rank-IC 收缩权重。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from math import exp, log

from app.services.quant_stats import rank_ic

FAMILIES = ("quality", "value", "momentum", "trend", "lowvol")
# 未经本地样本证明的因子不预设正 Alpha。训练成熟前仍使用结构先验权重；
# 成熟后只把观测 IC 向零收缩，负证据可以把因子目标权重降为零。
NEUTRAL_PRIOR_IC = {family: 0.0 for family in FAMILIES}
PRIOR_FAMILY_WEIGHTS = {
    "quality": 0.30,
    "value": 0.25,
    "momentum": 0.20,
    "trend": 0.15,
    "lowvol": 0.10,
}


@dataclass(frozen=True)
class IcTrainingObservation:
    signal_date: date
    label_available_at: date
    family_scores: dict[str, dict[str, float | None]]
    forward_returns: dict[str, float]


@dataclass(frozen=True)
class IcWeightEstimate:
    as_of: date
    weights: dict[str, float]
    raw_ic: dict[str, float | None]
    shrunk_ic: dict[str, float]
    observation_counts: dict[str, int]
    training_start: date | None
    training_end: date | None
    half_life_periods: float
    prior_strength: float
    maximum_weight: float
    minimum_weight: float
    previous_weight_blend: float
    status: str


def _bounded_weights(
    signals: dict[str, float],
    *,
    minimum_weight: float,
    maximum_weight: float,
) -> dict[str, float]:
    if (
        minimum_weight < 0
        or maximum_weight <= 0
        or minimum_weight > maximum_weight
        or minimum_weight * len(FAMILIES) > 1.0 + 1e-12
        or maximum_weight * len(FAMILIES) < 1.0 - 1e-12
    ):
        raise ValueError("infeasible family weight bounds")
    positive = {
        family: max(float(signals.get(family, 0.0)), 0.0) for family in FAMILIES
    }
    if sum(positive.values()) <= 0:
        positive = dict(PRIOR_FAMILY_WEIGHTS)
    elif sum(value > 0 for value in positive.values()) * maximum_weight < 1.0 - 1e-12:
        # 正信号过少时强行满足上限没有可行解；继续用分散结构先验，
        # 等待更多因子获得成熟正证据。
        positive = dict(PRIOR_FAMILY_WEIGHTS)
    low = 0.0
    high = 1.0
    while (
        sum(
            min(max(high * positive[name], minimum_weight), maximum_weight)
            for name in FAMILIES
        )
        < 1.0
    ):
        high *= 2.0
    for _ in range(100):
        middle = (low + high) / 2.0
        total = sum(
            min(
                max(middle * positive[name], minimum_weight),
                maximum_weight,
            )
            for name in FAMILIES
        )
        if total < 1.0:
            low = middle
        else:
            high = middle
    weights = {
        name: min(
            max(high * positive[name], minimum_weight),
            maximum_weight,
        )
        for name in FAMILIES
    }
    total = sum(weights.values())
    return {name: value / total for name, value in weights.items()}


def estimate_ic_weights(
    observations: list[IcTrainingObservation],
    *,
    as_of: date,
    half_life_periods: float = 12.0,
    prior_strength: float = 12.0,
    maximum_weight: float = 0.50,
    minimum_weight: float = 0.0,
    minimum_periods: int = 12,
    previous_weights: dict[str, float] | None = None,
    previous_weight_blend: float = 0.50,
) -> IcWeightEstimate:
    """未来标签未成熟（label_available_at > as_of）的观察绝不参与。"""
    usable = sorted(
        (
            item
            for item in observations
            if item.label_available_at <= as_of and item.signal_date < as_of
        ),
        key=lambda item: item.signal_date,
    )
    ic_by_family: dict[str, list[tuple[int, float]]] = {
        family: [] for family in FAMILIES
    }
    for age, item in enumerate(reversed(usable)):
        for family in FAMILIES:
            scores = item.family_scores.get(family, {})
            codes = [
                code
                for code, value in scores.items()
                if value is not None and code in item.forward_returns
            ]
            if len(codes) < 5:
                continue
            value = rank_ic(
                [float(scores[code]) for code in codes],
                [item.forward_returns[code] for code in codes],
            )
            if value is not None:
                ic_by_family[family].append((age, value))
    decay = log(2.0) / max(half_life_periods, 1e-9)
    raw_ic: dict[str, float | None] = {}
    shrunk: dict[str, float] = {}
    counts: dict[str, int] = {}
    for family in FAMILIES:
        samples = ic_by_family[family]
        counts[family] = len(samples)
        if samples:
            sample_weights = [exp(-decay * age) for age, _value in samples]
            weighted_mean = sum(
                weight * value
                for weight, (_age, value) in zip(sample_weights, samples, strict=True)
            ) / sum(sample_weights)
            raw_ic[family] = weighted_mean
            effective_n = sum(sample_weights)
        else:
            raw_ic[family] = None
            weighted_mean = 0.0
            effective_n = 0.0
        shrunk[family] = (
            effective_n * weighted_mean + prior_strength * NEUTRAL_PRIOR_IC[family]
        ) / (effective_n + prior_strength)
    enough = all(count >= minimum_periods for count in counts.values())
    if not enough:
        weights = dict(PRIOR_FAMILY_WEIGHTS)
        status = "robust_prior_fallback"
    else:
        target = _bounded_weights(
            shrunk,
            minimum_weight=minimum_weight,
            maximum_weight=maximum_weight,
        )
        if previous_weights is not None:
            blend = min(max(previous_weight_blend, 0.0), 1.0)
            previous = _bounded_weights(
                previous_weights,
                minimum_weight=minimum_weight,
                maximum_weight=maximum_weight,
            )
            weights = {
                family: (blend * previous[family] + (1.0 - blend) * target[family])
                for family in FAMILIES
            }
            status = "trained_shrunk_turnover_penalized"
        else:
            weights = target
            status = "trained_shrunk"
    return IcWeightEstimate(
        as_of=as_of,
        weights=weights,
        raw_ic=raw_ic,
        shrunk_ic=shrunk,
        observation_counts=counts,
        training_start=usable[0].signal_date if usable else None,
        training_end=usable[-1].label_available_at if usable else None,
        half_life_periods=half_life_periods,
        prior_strength=prior_strength,
        maximum_weight=maximum_weight,
        minimum_weight=minimum_weight,
        previous_weight_blend=previous_weight_blend,
        status=status,
    )
