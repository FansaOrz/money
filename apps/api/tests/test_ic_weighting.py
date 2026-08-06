"""滚动 Rank-IC 权重的收缩、上限和反未来标签测试。"""

from datetime import date

from app.services.ic_weighting import (
    FAMILIES,
    IcTrainingObservation,
    estimate_ic_weights,
)


def _observation(
    signal_month: int,
    available_month: int,
    *,
    reverse_quality: bool = False,
) -> IcTrainingObservation:
    codes = [f"{index:06d}" for index in range(20)]
    returns = {code: index / 100 for index, code in enumerate(codes)}
    family_scores = {
        family: {
            code: (
                -float(index)
                if family == "quality" and reverse_quality
                else float(index)
            )
            for index, code in enumerate(codes)
        }
        for family in FAMILIES
    }
    return IcTrainingObservation(
        signal_date=date(2025, signal_month, 1),
        label_available_at=date(2025, available_month, 1),
        family_scores=family_scores,
        forward_returns=returns,
    )


def test_future_label_is_excluded_and_weights_are_capped() -> None:
    known = _observation(1, 2)
    future_bad = _observation(2, 4, reverse_quality=True)
    as_of = date(2025, 3, 1)
    with_future = estimate_ic_weights([known, future_bad], as_of=as_of)
    without_future = estimate_ic_weights([known], as_of=as_of)
    assert with_future.weights == without_future.weights
    assert max(with_future.weights.values()) <= 0.30 + 1e-12
    assert min(with_future.weights.values()) >= 0.08 - 1e-12
    assert abs(sum(with_future.weights.values()) - 1.0) < 1e-12
    assert with_future.status == "robust_prior_fallback"
    assert with_future.weights == {
        "quality": 0.30,
        "value": 0.25,
        "momentum": 0.20,
        "trend": 0.15,
        "lowvol": 0.10,
    }


def test_weight_history_is_deterministic_and_shrunk_toward_prior() -> None:
    observations = [
        _observation(1, 2),
        _observation(2, 3),
        _observation(3, 4),
    ]
    first = estimate_ic_weights(
        observations,
        as_of=date(2025, 5, 1),
        minimum_periods=3,
    )
    second = estimate_ic_weights(
        observations,
        as_of=date(2025, 5, 1),
        minimum_periods=3,
    )
    assert first == second
    assert first.status == "trained_shrunk"
    assert first.observation_counts["quality"] == 3
    assert first.shrunk_ic["quality"] < 1.0


def test_previous_weight_blend_penalizes_weight_turnover() -> None:
    observations = [
        _observation(1, 2),
        _observation(2, 3),
        _observation(3, 4),
    ]
    unblended = estimate_ic_weights(
        observations,
        as_of=date(2025, 5, 1),
        minimum_periods=3,
        previous_weight_blend=0.0,
        previous_weights={
            "quality": 0.10,
            "value": 0.20,
            "momentum": 0.20,
            "trend": 0.20,
            "lowvol": 0.30,
        },
    )
    blended = estimate_ic_weights(
        observations,
        as_of=date(2025, 5, 1),
        minimum_periods=3,
        previous_weight_blend=0.75,
        previous_weights={
            "quality": 0.10,
            "value": 0.20,
            "momentum": 0.20,
            "trend": 0.20,
            "lowvol": 0.30,
        },
    )
    assert blended.status == "trained_shrunk_turnover_penalized"
    assert abs(blended.weights["lowvol"] - 0.30) < abs(
        unblended.weights["lowvol"] - 0.30
    )
