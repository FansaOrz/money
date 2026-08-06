"""历史涨跌停派生脚本的名称证据补全测试。"""

from datetime import date

from scripts.backfill_validated_price_limits import (
    complete_leading_name_period,
)


def test_public_ordered_names_fill_only_the_leading_dated_gap() -> None:
    periods = [
        (date(2021, 5, 6), date(2022, 5, 16), "*ST西水"),
        (date(2022, 5, 17), None, "退市西水"),
    ]

    completed, predecessor = complete_leading_name_period(
        periods,
        ["G西水", "西水股份", "*ST西水", "退市西水", "西创5"],
        date(2020, 1, 1),
    )

    assert predecessor == "西水股份"
    assert completed[0] == (
        date(2020, 1, 1),
        date(2021, 5, 5),
        "西水股份",
    )
    assert completed[1:] == periods


def test_leading_period_is_not_inferred_without_ordered_predecessor() -> None:
    periods = [(date(2021, 5, 6), None, "*ST西水")]

    completed, predecessor = complete_leading_name_period(
        periods,
        ["*ST西水", "退市西水"],
        date(2020, 1, 1),
    )

    assert completed == periods
    assert predecessor is None
