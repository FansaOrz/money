"""历史涨跌停派生脚本的名称证据补全测试。"""

from datetime import date

from scripts.backfill_validated_price_limits import (
    complete_leading_name_period,
    dated_name_periods,
    fetch_required_public_names,
    public_names_confirm,
    read_public_name_cache,
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


def test_no_public_name_request_when_no_dated_name_needs_crosscheck(
    tmp_path,
) -> None:
    cache = tmp_path / "public-names.json"

    result = fetch_required_public_names(
        [],
        cache_path=cache,
        workers=2,
        timeout_seconds=1,
    )

    assert result == {}
    assert not cache.exists()


def test_public_name_cache_is_reused_without_network(tmp_path) -> None:
    cache = tmp_path / "public-names.json"
    cache.write_text(
        '{"entries":{"600291":{"names":["西水股份","*ST西水"]}}}',
        encoding="utf-8",
    )

    result = fetch_required_public_names(
        ["600291"],
        cache_path=cache,
        workers=1,
        timeout_seconds=1,
    )

    assert result == {"600291": ["西水股份", "*ST西水"]}
    assert read_public_name_cache(cache) == result


def test_single_name_from_listing_is_closed_by_stock_basic(tmp_path) -> None:
    import pandas as pd

    snapshot = tmp_path / "snapshot"
    name_dir = snapshot / "stocks" / "namechange"
    basic_dir = snapshot / "global" / "stock_basic"
    name_dir.mkdir(parents=True)
    basic_dir.mkdir(parents=True)
    name_path = name_dir / "603290.SH.parquet"
    basic_path = basic_dir / "L.parquet"
    pd.DataFrame(
        [
            {
                "ts_code": "603290.SH",
                "name": "斯达半导",
                "start_date": "20200204",
                "end_date": None,
            }
        ]
    ).to_parquet(name_path, index=False)
    pd.DataFrame(
        [
            {
                "ts_code": "603290.SH",
                "name": "斯达半导",
                "list_date": "20200204",
            }
        ]
    ).to_parquet(basic_path, index=False)

    periods, sources, mode = dated_name_periods(snapshot, "603290.SH")

    assert periods == [(date(2020, 2, 4), None, "斯达半导")]
    assert sources == (name_path, basic_path)
    assert mode == ("tushare.namechange.dated_single_from_listing+stock_basic.current")


def test_duplicate_same_name_records_from_listing_still_form_closed_evidence(
    tmp_path,
) -> None:
    import pandas as pd

    snapshot = tmp_path / "snapshot"
    name_dir = snapshot / "stocks" / "namechange"
    basic_dir = snapshot / "global" / "stock_basic"
    name_dir.mkdir(parents=True)
    basic_dir.mkdir(parents=True)
    pd.DataFrame(
        [
            {"name": "沪硅产业", "start_date": "20230412", "end_date": None},
            {"name": "沪硅产业", "start_date": "20200420", "end_date": None},
        ]
    ).to_parquet(name_dir / "688126.SH.parquet", index=False)
    pd.DataFrame(
        [
            {
                "ts_code": "688126.SH",
                "name": "沪硅产业",
                "list_date": "20200420",
            }
        ]
    ).to_parquet(basic_dir / "L.parquet", index=False)

    periods, _sources, mode = dated_name_periods(snapshot, "688126.SH")

    assert len(periods) == 2
    assert mode == ("tushare.namechange.dated_single_from_listing+stock_basic.current")


def test_single_name_from_listing_can_use_delisted_stock_basic(tmp_path) -> None:
    import pandas as pd

    snapshot = tmp_path / "snapshot"
    name_dir = snapshot / "stocks" / "namechange"
    basic_dir = snapshot / "global" / "stock_basic"
    name_dir.mkdir(parents=True)
    basic_dir.mkdir(parents=True)
    pd.DataFrame(
        [{"name": "德邦股份", "start_date": "20180116", "end_date": None}]
    ).to_parquet(name_dir / "603056.SH.parquet", index=False)
    pd.DataFrame(
        [
            {
                "ts_code": "603056.SH",
                "name": "德邦股份",
                "list_date": "20180116",
            }
        ]
    ).to_parquet(basic_dir / "D.parquet", index=False)

    periods, sources, mode = dated_name_periods(snapshot, "603056.SH")

    assert periods == [(date(2018, 1, 16), None, "德邦股份")]
    assert sources[-1] == basic_dir / "D.parquet"
    assert mode == ("tushare.namechange.dated_single_from_listing+stock_basic.current")


def test_public_name_crosscheck_accepts_only_parenthesized_b_share_suffix() -> None:
    assert public_names_confirm(
        {"海航创新", "*ST海创"},
        {"海航创新（海创B股）", "*ST海创（*ST海创B）"},
    )
    assert not public_names_confirm({"海航创新"}, {"前海航创新产业"})
