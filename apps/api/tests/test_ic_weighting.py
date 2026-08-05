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
    assert max(with_future.weights.values()) <= 0.35 + 1e-12
    assert abs(sum(with_future.weights.values()) - 1.0) < 1e-12


def test_weight_history_is_deterministic_and_shrunk_toward_prior() -> None:
    observations = [
        _observation(1, 2),
        _observation(2, 3),
        _observation(3, 4),
    ]
    first = estimate_ic_weights(observations, as_of=date(2025, 5, 1))
    second = estimate_ic_weights(observations, as_of=date(2025, 5, 1))
    assert first == second
    assert first.status == "trained"
    assert first.observation_counts["quality"] == 3
    assert first.shrunk_ic["quality"] < 1.0
