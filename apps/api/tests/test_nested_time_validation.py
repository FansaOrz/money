"""标签期驱动的 purge/embargo 和训练变换隔离测试。"""

from datetime import date, timedelta

import pytest

from app.services.nested_time_validation import (
    assert_transform_fit_is_train_only,
    nested_expanding_folds,
    validate_label_gap,
)


def test_gap_must_cover_label_holding_period() -> None:
    with pytest.raises(ValueError, match="purge"):
        validate_label_gap(
            label_holding_days=21,
            purge_days=10,
            embargo_days=21,
        )


def test_outer_test_never_participates_in_inner_transform_fit() -> None:
    start = date(2020, 1, 1)
    days = [start + timedelta(days=index) for index in range(600)]
    folds = nested_expanding_folds(
        days,
        label_holding_days=21,
        purge_days=21,
        embargo_days=21,
        minimum_train_days=252,
        validation_days=63,
        test_days=63,
    )
    assert folds
    fold = folds[0]
    assert max(fold.train) < min(fold.validation) < min(fold.test)
    assert_transform_fit_is_train_only(fold, list(fold.train))
    with pytest.raises(ValueError, match="测试外"):
        assert_transform_fit_is_train_only(
            fold, [*fold.train, fold.test[0]]
        )
