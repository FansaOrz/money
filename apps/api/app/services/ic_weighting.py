"""仅使用已成熟训练标签的滚动 Rank-IC 收缩权重。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from math import exp, log

from app.services.quant_stats import rank_ic

FAMILIES = ("quality", "value", "momentum", "trend", "lowvol")
ROBUST_PRIOR_IC = {
    "quality": 0.020,
    "value": 0.015,
    "momentum": 0.015,
    "trend": 0.005,
    "lowvol": 0.010,
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
    status: str


def estimate_ic_weights(
    observations: list[IcTrainingObservation],
    *,
    as_of: date,
    half_life_periods: float = 12.0,
    prior_strength: float = 12.0,
    maximum_weight: float = 0.35,
    minimum_periods: int = 3,
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
                for weight, (_age, value) in zip(
                    sample_weights, samples, strict=True
                )
            ) / sum(sample_weights)
            raw_ic[family] = weighted_mean
            effective_n = sum(sample_weights)
        else:
            raw_ic[family] = None
            weighted_mean = 0.0
            effective_n = 0.0
        shrunk[family] = (
            effective_n * weighted_mean
            + prior_strength * ROBUST_PRIOR_IC[family]
        ) / (effective_n + prior_strength)
    positive = {family: max(value, 0.0) for family, value in shrunk.items()}
    total = sum(positive.values())
    if total <= 0:
        weights = {family: 0.0 for family in FAMILIES}
    else:
        weights = {family: value / total for family, value in positive.items()}
        for _ in range(len(FAMILIES) + 1):
            capped = {
                family
                for family, value in weights.items()
                if value > maximum_weight + 1e-15
            }
            if not capped:
                break
            fixed = maximum_weight * len(capped)
            remaining_names = [name for name in FAMILIES if name not in capped]
            remaining_raw = sum(positive[name] for name in remaining_names)
            for name in capped:
                weights[name] = maximum_weight
            for name in remaining_names:
                weights[name] = (
                    (1.0 - fixed) * positive[name] / remaining_raw
                    if remaining_raw > 0
                    else 0.0
                )
    enough = all(count >= minimum_periods for count in counts.values())
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
        status="trained" if enough else "robust_prior_shrunk",
    )
