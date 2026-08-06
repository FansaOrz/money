"""批量跨源复核的来源身份与日期工具测试。"""

from datetime import UTC, date, datetime

from app.services.source_reconciliation_batch import (
    _as_date,
    _source_root,
    _unique_source_candidates,
)


def test_import_alias_cannot_masquerade_as_independent_source() -> None:
    assert _unique_source_candidates(
        [
            ("tushare", 10.0),
            ("stocktoday", 10.0),
            ("tencent_close", 10.1),
        ]
    ) == [("tushare", 10.0), ("tencent", 10.1)]


def test_source_root_preserves_comparable_taxonomy_identity() -> None:
    assert _source_root("cninfo_profile") == "cninfo"
    assert _source_root("cninfo:taxonomy_crosswalk:电池") == "cninfo"
    assert _source_root("stocktoday_sw2021") == "stocktoday_sw2021"
    assert _source_root("baidu_trade_notice") == "baidu"


def test_effective_date_normalization_drops_time_component() -> None:
    assert _as_date(datetime(2026, 8, 5, 8, tzinfo=UTC)) == date(2026, 8, 5)


def test_same_source_corporate_action_duplicates_are_not_cross_source() -> None:
    assert _unique_source_candidates(
        [
            ("tushare:dividend", {"cash_div": 1.0}),
            ("tushare:dividend", {"cash_div": 2.0}),
            ("stocktoday", {"cash_div": 1.0}),
            ("baidu_trade_notice", {"cash_div": 1.0}),
        ]
    ) == [
        ("tushare", {"cash_div": 1.0}),
        ("baidu", {"cash_div": 1.0}),
    ]
