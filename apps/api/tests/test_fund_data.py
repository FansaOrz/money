"""基金历史净值断点回填测试。

覆盖：
- 东财 lsjz 分页：按 TotalCount / cutoff 翻页终止、去重、排序；
- 单页请求失败重试 3 次，成功后继续；
- 只 upsert 缺失日期；已有最新数据不被 AKShare 回退覆盖；
- AKShare fund_open_fund_info_em 回退与 968xxx fund_hk_fund_hist_em 回退；
- 数据覆盖状态（complete/partial/failed）与断点续传游标；
- days/years 参数上限 5 年。
"""

from datetime import date, timedelta
from decimal import Decimal

import pandas as pd
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import FundNav, Instrument, NavSyncStatus
from app.services import fund_data
from app.services.fund_data import (
    PAGE_SIZE,
    backfill_fund_nav_history,
    fetch_nav_history,
    fetch_nav_history_fast,
    fetch_nav_history_with_fallback,
    resolve_window,
    sync_fund_nav_history,
    upsert_nav_rows,
)


def _make_lsjz_page(rows: list[tuple[str, str]], total: int) -> dict:
    """构造东财 lsjz 接口响应。rows: (FSRQ, DWJZ)"""
    return {
        "ErrCode": 0,
        "TotalCount": total,
        "Data": {
            "LSJZList": [
                {"FSRQ": day, "DWJZ": nav, "LJJZ": nav, "JZZZL": "0.5"}
                for day, nav in rows
            ],
            "TotalCount": total,
        },
    }


def _daily_rows(end: date, count: int) -> list[tuple[str, str]]:
    """生成 count 条连续日期的净值行（倒序，与东财接口一致）。"""
    return [
        ((end - timedelta(days=offset)).isoformat(), f"1.{offset:04d}")
        for offset in range(count)
    ]


@pytest.fixture()
def instrument(db_session: Session) -> Instrument:
    fund = Instrument(code="110022", name="易方达消费行业股票")
    db_session.add(fund)
    db_session.commit()
    return fund


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """测试中跳过限速与重试退避的 sleep。"""
    monkeypatch.setattr(fund_data.time, "sleep", lambda *_args, **_kwargs: None)


class TestResolveWindow:
    def test_defaults_to_one_year(self) -> None:
        days, cutoff = resolve_window(today=date(2026, 7, 31))
        assert days == 365
        assert cutoff == date(2025, 7, 31)

    def test_years_capped_at_five(self) -> None:
        days, cutoff = resolve_window(years=10, today=date(2026, 7, 31))
        assert days <= 366 * 5
        assert cutoff >= date(2021, 7, 25)

    def test_days_capped_at_five_years(self) -> None:
        days, _ = resolve_window(days=99999, today=date(2026, 7, 31))
        assert days == 366 * 5


class TestFetchNavHistoryFast:
    def test_parses_unit_and_accumulated_history(self, monkeypatch: pytest.MonkeyPatch) -> None:
        today = date.today()
        first = int((today - timedelta(days=2)).strftime("%s")) * 1000
        second = int((today - timedelta(days=1)).strftime("%s")) * 1000
        text = (
            'var Data_netWorthTrend = '
            f'[{{"x":{first},"y":1.1,"equityReturn":0.5}},'
            f'{{"x":{second},"y":1.2,"equityReturn":0.6}}];'
            f'var Data_ACWorthTrend = [[{first},1.3],[{second},1.4]];'
        )
        monkeypatch.setattr(fund_data, "_request_text_with_retry", lambda *a, **k: text)
        rows, error = fetch_nav_history_fast("110022", days=30)
        assert error is None
        assert len(rows) == 2
        assert rows[0]["unit_nav"] == Decimal("1.1")
        assert rows[0]["accumulated_nav"] == Decimal("1.3")
        assert rows[0]["source"] == "eastmoney_fast"

    def test_uses_eastmoney_china_calendar_date(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """东财北京时间零点不能按 UTC date 解析成前一天。"""
        # 2026-07-27 00:00:00 Asia/Shanghai = 2026-07-26 16:00:00 UTC
        timestamp = 1785081600000
        text = (
            "var Data_netWorthTrend = "
            f'[{{"x":{timestamp},"y":2.7901,"equityReturn":0.65}}];'
            f"var Data_ACWorthTrend = [[{timestamp},3.0237]];"
        )
        monkeypatch.setattr(fund_data, "_request_text_with_retry", lambda *a, **k: text)
        rows, error = fetch_nav_history_fast("008401", days=3650)
        assert error is None
        assert rows[0]["nav_date"] == date(2026, 7, 27)

    def test_reports_missing_trend(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            fund_data,
            "_request_text_with_retry",
            lambda *a, **k: "<!doctype html><title>页面未找到</title>",
        )
        rows, error = fetch_nav_history_fast("968092", years=5)
        assert rows == []
        assert error is not None and "Data_netWorthTrend" in error


class TestFetchNavHistory:
    def test_paginates_until_cutoff(self, monkeypatch: pytest.MonkeyPatch) -> None:
        today = date.today()
        calls: list[int] = []
        # TotalCount 足够大，保证翻页只能由 cutoff 触发终止
        pages = {
            1: _make_lsjz_page(_daily_rows(today, PAGE_SIZE), total=10000),
            2: _make_lsjz_page(_daily_rows(today - timedelta(days=PAGE_SIZE), PAGE_SIZE), total=10000),
            3: _make_lsjz_page(_daily_rows(today - timedelta(days=2 * PAGE_SIZE), PAGE_SIZE), total=10000),
        }

        def fake_request(url: str, code: str, timeout: int = 15) -> dict | None:
            page = int(url.split("pageIndex=")[1].split("&")[0])
            calls.append(page)
            return pages.get(page)

        monkeypatch.setattr(fund_data, "_request_json", fake_request)
        rows, error = fetch_nav_history("110022", days=30)
        assert error is None
        # 第 2 页已含 cutoff 之前的数据，应在第 2 页后终止
        assert calls == [1, 2]
        assert rows == sorted(rows, key=lambda item: item["nav_date"])
        assert len({row["nav_date"] for row in rows}) == len(rows)
        assert all(row["nav_date"] >= today - timedelta(days=30) for row in rows)

    def test_stops_at_total_count(self, monkeypatch: pytest.MonkeyPatch) -> None:
        today = date.today()
        calls: list[int] = []

        def fake_request(url: str, code: str, timeout: int = 15) -> dict | None:
            page = int(url.split("pageIndex=")[1].split("&")[0])
            calls.append(page)
            # 总共只有 40 条，两页拿满 TotalCount 即停
            return _make_lsjz_page(
                _daily_rows(today - timedelta(days=(page - 1) * PAGE_SIZE), PAGE_SIZE),
                total=2 * PAGE_SIZE,
            )

        monkeypatch.setattr(fund_data, "_request_json", fake_request)
        rows, error = fetch_nav_history("110022", years=5)
        assert error is None
        assert calls == [1, 2]
        assert len(rows) == 2 * PAGE_SIZE

    def test_retries_failed_page(self, monkeypatch: pytest.MonkeyPatch) -> None:
        today = date.today()
        attempts: list[int] = []

        def fake_request(url: str, code: str, timeout: int = 15) -> dict | None:
            page = int(url.split("pageIndex=")[1].split("&")[0])
            attempts.append(page)
            if page == 1 and len(attempts) < 3:
                return None  # 前两次失败，第三次成功
            return _make_lsjz_page(_daily_rows(today, 5), total=5)

        monkeypatch.setattr(fund_data, "_request_json", fake_request)
        rows, error = fetch_nav_history("110022", days=30)
        assert error is None
        assert attempts == [1, 1, 1]
        assert len(rows) == 5

    def test_gives_up_after_max_retries(self, monkeypatch: pytest.MonkeyPatch) -> None:
        attempts: list[int] = []

        def fake_request(url: str, code: str, timeout: int = 15) -> dict | None:
            attempts.append(1)
            return None

        monkeypatch.setattr(fund_data, "_request_json", fake_request)
        rows, error = fetch_nav_history("110022", days=30)
        assert rows == []
        assert error is not None
        assert len(attempts) == fund_data.MAX_RETRIES

    def test_end_date_resumes_before_cursor(self, monkeypatch: pytest.MonkeyPatch) -> None:
        today = date.today()
        cursor = today - timedelta(days=10)

        def fake_request(url: str, code: str, timeout: int = 15) -> dict | None:
            return _make_lsjz_page(_daily_rows(today, 15), total=15)

        monkeypatch.setattr(fund_data, "_request_json", fake_request)
        rows, _ = fetch_nav_history("110022", days=30, end_date=cursor)
        assert all(row["nav_date"] < cursor for row in rows)


class TestUpsertNavRows:
    def test_inserts_only_missing_dates(self, db_session: Session, instrument: Instrument) -> None:
        existing = FundNav(
            instrument_id=instrument.id,
            nav_date=date(2026, 7, 30),
            unit_nav=Decimal("2.0000"),
            source="eastmoney",
        )
        db_session.add(existing)
        db_session.commit()

        rows = [
            {
                "code": instrument.code,
                "nav_date": date(2026, 7, 30),
                "unit_nav": Decimal("2.1000"),
                "accumulated_nav": None,
                "daily_growth_rate": None,
                "source": "eastmoney",
            },
            {
                "code": instrument.code,
                "nav_date": date(2026, 7, 31),
                "unit_nav": Decimal("2.1100"),
                "accumulated_nav": None,
                "daily_growth_rate": None,
                "source": "eastmoney",
            },
        ]
        inserted, updated = upsert_nav_rows(db_session, instrument, rows)
        db_session.commit()
        assert (inserted, updated) == (1, 1)
        db_session.refresh(existing)
        # 东财主源允许修正历史值
        assert existing.unit_nav == Decimal("2.1000")

    def test_akshare_fallback_does_not_overwrite_existing(
        self, db_session: Session, instrument: Instrument
    ) -> None:
        existing = FundNav(
            instrument_id=instrument.id,
            nav_date=date(2026, 7, 30),
            unit_nav=Decimal("2.0000"),
            source="eastmoney",
        )
        db_session.add(existing)
        db_session.commit()

        rows = [
            {
                "code": instrument.code,
                "nav_date": date(2026, 7, 30),
                "unit_nav": Decimal("9.9999"),
                "accumulated_nav": None,
                "daily_growth_rate": None,
                "source": "akshare",
            },
            {
                "code": instrument.code,
                "nav_date": date(2026, 7, 29),
                "unit_nav": Decimal("1.9900"),
                "accumulated_nav": None,
                "daily_growth_rate": None,
                "source": "akshare",
            },
        ]
        inserted, updated = upsert_nav_rows(db_session, instrument, rows)
        db_session.commit()
        assert (inserted, updated) == (1, 0)
        db_session.refresh(existing)
        assert existing.unit_nav == Decimal("2.0000")
        assert existing.source == "eastmoney"


def _akshare_frame(end: date, count: int) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "净值日期": end - timedelta(days=offset),
                "单位净值": 1.5 + offset / 1000,
                "日增长率": 0.1,
            }
            for offset in range(count)
        ]
    )


class TestFallback:
    @pytest.fixture(autouse=True)
    def no_fast_source(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            fund_data,
            "fetch_nav_history_fast",
            lambda *a, **k: ([], "快速源不可用"),
        )

    def test_akshare_fallback_when_eastmoney_fails(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(fund_data, "_request_json", lambda *a, **k: None)
        monkeypatch.setattr(
            fund_data, "_fetch_nav_history_akshare",
            lambda code, years=5: (
                [
                    {
                        "code": code,
                        "nav_date": date(2026, 7, 30),
                        "unit_nav": Decimal("1.5"),
                        "accumulated_nav": None,
                        "daily_growth_rate": None,
                        "source": "akshare",
                    }
                ],
                None,
            ),
        )
        rows, error, source = fetch_nav_history_with_fallback("110022", years=5)
        assert source == "akshare"
        assert error is None
        assert len(rows) == 1

    def test_hk_fallback_for_968_codes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(fund_data, "_request_json", lambda *a, **k: None)
        monkeypatch.setattr(
            fund_data, "_fetch_nav_history_akshare", lambda code, years=5: ([], "open fallback 空数据")
        )

        def fake_hk(code: str):
            return (
                [
                    {
                        "code": code,
                        "nav_date": date(2026, 7, 30),
                        "unit_nav": Decimal("10.5"),
                        "accumulated_nav": None,
                        "daily_growth_rate": None,
                        "source": "akshare_hk",
                    }
                ],
                None,
            )

        monkeypatch.setattr(fund_data, "_fetch_nav_history_akshare_hk", fake_hk)
        rows, error, source = fetch_nav_history_with_fallback("968092", years=5)
        assert source == "akshare_hk"
        assert rows and rows[0]["unit_nav"] == Decimal("10.5")

    def test_all_sources_fail_reports_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(fund_data, "_request_json", lambda *a, **k: None)
        monkeypatch.setattr(
            fund_data, "_fetch_nav_history_akshare", lambda code, years=5: ([], "akshare 炸了")
        )
        monkeypatch.setattr(
            fund_data, "_fetch_nav_history_akshare_hk", lambda code: ([], "hk 也炸了")
        )
        rows, error, source = fetch_nav_history_with_fallback("110022", years=5)
        assert rows == []
        assert source is None
        assert error is not None

    def test_fallback_disabled(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(fund_data, "_request_json", lambda *a, **k: None)
        called = []
        monkeypatch.setattr(
            fund_data,
            "_fetch_nav_history_akshare",
            lambda code, years=5: called.append(code) or ([], None),
        )
        rows, error, source = fetch_nav_history_with_fallback("110022", years=5, use_fallback=False)
        assert rows == []
        assert called == []


class TestBackfill:
    def test_complete_status_when_reaching_target(
        self, db_session: Session, instrument: Instrument, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        today = date.today()
        target_start = today - timedelta(days=int(5 * 365.25))

        def fake_fetch(code, days=None, years=None, end_date=None, use_fallback=True, timeout=15):
            rows = [
                {
                    "code": code,
                    "nav_date": target_start - timedelta(days=1),
                    "unit_nav": Decimal("1.0"),
                    "accumulated_nav": None,
                    "daily_growth_rate": None,
                    "source": "eastmoney",
                },
                {
                    "code": code,
                    "nav_date": today,
                    "unit_nav": Decimal("1.5"),
                    "accumulated_nav": None,
                    "daily_growth_rate": None,
                    "source": "eastmoney",
                },
            ]
            return rows, None, "eastmoney"

        monkeypatch.setattr(fund_data, "fetch_nav_history_with_fallback", fake_fetch)
        result = backfill_fund_nav_history(db_session, instrument, years=5)
        assert result["status"] == "complete"
        assert result["inserted"] == 2
        record = db_session.scalar(
            select(NavSyncStatus).where(NavSyncStatus.instrument_id == instrument.id)
        )
        assert record is not None
        assert record.status == "complete"
        assert record.earliest_nav_date == target_start - timedelta(days=1)
        assert record.latest_nav_date == today
        assert record.next_end_date is None
        assert record.row_count == 2

    def test_partial_status_sets_resume_cursor(
        self, db_session: Session, instrument: Instrument, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        today = date.today()
        earliest = today - timedelta(days=365)  # 只回填了 1 年，未到 5 年目标

        def fake_fetch(code, days=None, years=None, end_date=None, use_fallback=True, timeout=15):
            return (
                [
                    {
                        "code": code,
                        "nav_date": earliest,
                        "unit_nav": Decimal("1.0"),
                        "accumulated_nav": None,
                        "daily_growth_rate": None,
                        "source": "eastmoney",
                    }
                ],
                None,
                "eastmoney",
            )

        monkeypatch.setattr(fund_data, "fetch_nav_history_with_fallback", fake_fetch)
        result = backfill_fund_nav_history(db_session, instrument, years=5)
        assert result["status"] == "partial"
        record = db_session.scalar(
            select(NavSyncStatus).where(NavSyncStatus.instrument_id == instrument.id)
        )
        assert record is not None
        assert record.status == "partial"
        assert record.next_end_date == earliest

    def test_resume_uses_cursor(
        self, db_session: Session, instrument: Instrument, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        today = date.today()
        cursor = today - timedelta(days=365)
        db_session.add(
            NavSyncStatus(
                instrument_id=instrument.id,
                status="partial",
                target_start_date=today - timedelta(days=int(5 * 365.25)),
                next_end_date=cursor,
            )
        )
        db_session.commit()

        seen: dict[str, date | None] = {}

        def fake_fetch(code, days=None, years=None, end_date=None, use_fallback=True, timeout=15):
            seen["end_date"] = end_date
            return (
                [
                    {
                        "code": code,
                        "nav_date": cursor - timedelta(days=1500),
                        "unit_nav": Decimal("1.0"),
                        "accumulated_nav": None,
                        "daily_growth_rate": None,
                        "source": "eastmoney",
                    }
                ],
                None,
                "eastmoney",
            )

        monkeypatch.setattr(fund_data, "fetch_nav_history_with_fallback", fake_fetch)
        result = backfill_fund_nav_history(db_session, instrument, years=5)
        assert seen["end_date"] == cursor
        assert result["status"] == "complete"

    def test_completed_fund_is_skipped(
        self, db_session: Session, instrument: Instrument, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        today = date.today()
        db_session.add(
            NavSyncStatus(
                instrument_id=instrument.id,
                status="complete",
                target_start_date=today - timedelta(days=int(5 * 365.25)),
                earliest_nav_date=today - timedelta(days=int(5 * 365.25)),
                latest_nav_date=today,
            )
        )
        db_session.commit()
        called = []
        monkeypatch.setattr(
            fund_data,
            "fetch_nav_history_with_fallback",
            lambda *a, **k: called.append(a) or ([], None, None),
        )
        result = backfill_fund_nav_history(db_session, instrument, years=5)
        assert result["status"] == "skipped"
        assert called == []

    def test_existing_coverage_turns_failed_status_complete(
        self, db_session: Session, instrument: Instrument, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        today = date.today()
        target = today - timedelta(days=int(5 * 365.25))
        db_session.add(
            FundNav(
                instrument_id=instrument.id,
                nav_date=target - timedelta(days=1),
                unit_nav=Decimal("1.0"),
                source="eastmoney",
            )
        )
        db_session.add(
            NavSyncStatus(
                instrument_id=instrument.id,
                status="failed",
                target_start_date=target,
                earliest_nav_date=target - timedelta(days=1),
                latest_nav_date=today,
                last_error="旧任务误报",
            )
        )
        db_session.commit()
        monkeypatch.setattr(
            fund_data,
            "fetch_nav_history_with_fallback",
            lambda *a, **k: ([], "没有更早数据", None),
        )
        result = backfill_fund_nav_history(db_session, instrument, years=5)
        assert result["status"] == "complete"
        record = db_session.scalar(
            select(NavSyncStatus).where(NavSyncStatus.instrument_id == instrument.id)
        )
        assert record is not None and record.status == "complete"
        assert record.last_error is None

    def test_failure_recorded_in_status(
        self, db_session: Session, instrument: Instrument, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            fund_data,
            "fetch_nav_history_with_fallback",
            lambda *a, **k: ([], "网络超时", None),
        )
        result = backfill_fund_nav_history(db_session, instrument, years=5)
        assert result["status"] == "failed"
        assert result["error"] == "网络超时"
        record = db_session.scalar(
            select(NavSyncStatus).where(NavSyncStatus.instrument_id == instrument.id)
        )
        assert record is not None
        assert record.status == "failed"
        assert record.last_error == "网络超时"


class TestSyncFundNavHistory:
    def test_aggregates_results(
        self, db_session: Session, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        for code in ("110022", "968092"):
            db_session.add(Instrument(code=code, name=f"基金{code}"))
        db_session.commit()

        def fake_backfill(db, instrument, years=5, resume=True, use_fallback=True):
            if instrument.code == "110022":
                return {"code": instrument.code, "status": "complete", "inserted": 100, "updated": 0, "rows": 100, "error": None}
            return {"code": instrument.code, "status": "failed", "inserted": 0, "updated": 0, "rows": 0, "error": "炸"}

        monkeypatch.setattr(fund_data, "backfill_fund_nav_history", fake_backfill)
        result = sync_fund_nav_history(db_session, years=5)
        assert result["total_funds"] == 2
        assert result["completed"] == 1
        assert result["failed"] == 1
        assert result["rows"] == 100
        assert len(result["failures"]) == 1
        assert result["failures"][0]["code"] == "968092"

    def test_days_converted_to_capped_years(
        self, db_session: Session, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        db_session.add(Instrument(code="110022", name="基金"))
        db_session.commit()
        seen: dict[str, int] = {}

        def fake_backfill(db, instrument, years=5, resume=True, use_fallback=True):
            seen["years"] = years
            return {"code": instrument.code, "status": "complete", "inserted": 0, "updated": 0, "rows": 0, "error": None}

        monkeypatch.setattr(fund_data, "backfill_fund_nav_history", fake_backfill)
        sync_fund_nav_history(db_session, days=30)
        assert seen["years"] == 1
        sync_fund_nav_history(db_session, days=9999)
        assert seen["years"] == 5
