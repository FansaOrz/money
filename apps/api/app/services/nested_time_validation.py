"""严格嵌套的时间序列外层评估、内层选择与标签期 purge/embargo。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True)
class TimeFold:
    train: tuple[date, ...]
    validation: tuple[date, ...]
    test: tuple[date, ...]
    purge_days: int
    embargo_days: int


def validate_label_gap(
    *,
    label_holding_days: int,
    purge_days: int,
    embargo_days: int,
) -> None:
    if label_holding_days <= 0:
        raise ValueError("标签持有期必须为正")
    if purge_days < label_holding_days:
        raise ValueError("purge 长度不得短于标签持有期")
    if embargo_days < label_holding_days:
        raise ValueError("embargo 长度不得短于标签持有期")


def nested_expanding_folds(
    days: list[date],
    *,
    label_holding_days: int,
    purge_days: int,
    embargo_days: int,
    minimum_train_days: int = 252,
    validation_days: int = 63,
    test_days: int = 63,
) -> list[TimeFold]:
    validate_label_gap(
        label_holding_days=label_holding_days,
        purge_days=purge_days,
        embargo_days=embargo_days,
    )
    ordered = sorted(set(days))
    folds: list[TimeFold] = []
    cursor = minimum_train_days
    while True:
        validation_start = cursor + purge_days
        validation_end = validation_start + validation_days
        test_start = validation_end + embargo_days
        test_end = test_start + test_days
        if test_end > len(ordered):
            break
        folds.append(
            TimeFold(
                train=tuple(ordered[:cursor]),
                validation=tuple(ordered[validation_start:validation_end]),
                test=tuple(ordered[test_start:test_end]),
                purge_days=purge_days,
                embargo_days=embargo_days,
            )
        )
        cursor += test_days
    return folds


def assert_transform_fit_is_train_only(
    fold: TimeFold,
    fitted_dates: list[date],
) -> None:
    allowed = set(fold.train)
    leaked = sorted(set(fitted_dates) - allowed)
    if leaked:
        raise ValueError(
            f"预处理/中性化/权重拟合使用了测试外日期：{leaked[0]}"
        )
