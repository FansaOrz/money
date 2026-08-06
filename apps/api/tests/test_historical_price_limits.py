"""经验证的历史涨跌停派生规则测试。"""

from datetime import date

import pytest

from app.services.historical_price_limits import (
    dated_name_as_of,
    derive_price_limit,
    names_prove_non_st,
    round_price_tick,
)


def test_main_board_price_limit_uses_published_pre_close() -> None:
    result = derive_price_limit(
        "000685",
        date(2017, 5, 2),
        10.05,
        st=False,
    )

    assert result.up_limit == pytest.approx(11.06)
    assert result.down_limit == pytest.approx(9.05)
    assert result.rule_version == "SZSE_MAIN_10PCT"


def test_name_evidence_must_be_non_empty_and_never_st() -> None:
    assert names_prove_non_st(["公用科技", "G公用", "中山公用"])
    assert names_prove_non_st(["泰和新材"])
    assert not names_prove_non_st([])
    assert not names_prove_non_st(["普通名称", "*ST 风险"])
    assert not names_prove_non_st(["退市整理"])


def test_price_tick_rounding_is_deterministic() -> None:
    assert round_price_tick(11.055) == pytest.approx(11.06)
    assert round_price_tick(9.045) == pytest.approx(9.05)


def test_dated_name_evidence_allows_post_st_interval_without_losing_history() -> None:
    periods = [
        (date(2010, 4, 28), date(2011, 4, 13), "*ST鑫新"),
        (date(2011, 4, 14), None, "中文传媒"),
    ]
    assert dated_name_as_of(periods, date(2010, 5, 4)) == "*ST鑫新"
    assert dated_name_as_of(periods, date(2021, 9, 1)) == "中文传媒"
    assert dated_name_as_of(periods, date(2010, 4, 27)) is None
