"""调度器北京时间计划计算测试。"""

from datetime import datetime, timedelta
from unittest.mock import patch

from app.services.scheduler import next_run_times
from app.timezone import CN_TZ


def _at(hour: int, minute: int, day: int = 15, month: int = 7) -> datetime:
    return datetime(2026, month, day, hour, minute, tzinfo=CN_TZ)


def test_next_run_times_are_aware_beijing() -> None:
    times = next_run_times(_at(10, 0))
    for name, nxt in times.items():
        assert nxt.tzinfo is not None, name
        assert nxt.utcoffset() == timedelta(hours=8), name


def test_nav_schedule_same_day_when_before_deadline() -> None:
    times = next_run_times(_at(10, 0))
    nav = times["fund_nav"]
    assert (nav.hour, nav.minute) == (20, 30)
    assert nav.day == 15
    # paper 与净值同步同刻执行
    assert times["paper"] == nav


def test_nav_schedule_rolls_to_next_day_after_deadline() -> None:
    times = next_run_times(_at(21, 0))
    nav = times["fund_nav"]
    assert (nav.hour, nav.minute) == (20, 30)
    assert nav.day == 16


def test_indices_schedule_at_market_close() -> None:
    times = next_run_times(_at(16, 0))
    indices = times["indices"]
    assert (indices.hour, indices.minute) == (17, 30)
    assert indices.day == 15
    # 已过 17:30 则推到次日
    times_after = next_run_times(_at(18, 0))
    assert times_after["indices"].day == 16


def test_news_schedule_rolls_hourly() -> None:
    times = next_run_times(_at(10, 5))
    news = times["news"]
    assert (news.hour, news.minute) == (10, 17)
    # 已过当小时的 17 分则推到下一小时
    times_after = next_run_times(_at(10, 30))
    news_after = times_after["news"]
    assert (news_after.hour, news_after.minute) == (11, 17)


def test_us_indices_schedule() -> None:
    times = next_run_times(_at(6, 0))
    assert (times["us_indices"].hour, times["us_indices"].minute) == (7, 30)
    assert times["us_indices"].day == 15
    times_after = next_run_times(_at(8, 0))
    assert times_after["us_indices"].day == 16


def test_holdings_schedule_monthly_rollover() -> None:
    # 本月 1 号 19:05 已过，应排到下月 1 号
    times = next_run_times(_at(20, 0, day=15, month=7))
    holdings = times["holdings"]
    assert (holdings.month, holdings.day, holdings.hour, holdings.minute) == (8, 1, 19, 5)


def test_holdings_schedule_december_rollover() -> None:
    times = next_run_times(_at(20, 0, day=15, month=12))
    holdings = times["holdings"]
    assert (holdings.year, holdings.month, holdings.day) == (2027, 1, 1)


def test_candidate_pool_nav_schedule() -> None:
    times = next_run_times(_at(2, 0))
    backfill = times["candidate_pool_nav"]
    assert (backfill.hour, backfill.minute) == (3, 23)
    assert backfill.day == 15


def test_all_schedules_in_future() -> None:
    now = _at(23, 45, day=31, month=12)
    times = next_run_times(now)
    for name, nxt in times.items():
        assert nxt > now, name


# ---------------------------------------------------------------------------
# 任务包装：美股指数独立 job、stock_daily 断点续传批次
# ---------------------------------------------------------------------------

def test_us_indices_is_independent_job(db_session) -> None:
    """美股指数独立 job：写 us_indices 记录，且只同步 us 市场指数。"""
    from app.models.sync_run import SyncRun
    from app.services import scheduler
    from sqlalchemy import select

    captured: dict = {}

    def fake_sync(db, days: int = 30, markets: list[str] | None = None) -> dict:
        captured["markets"] = markets
        return {"total_indices": 2, "updated_indices": 2, "failed": 0, "errors": []}

    with patch("app.db.session.SessionLocal", return_value=db_session), \
         patch("app.services.scheduler.SessionLocal", return_value=db_session), \
         patch("app.services.index_data.sync_index_history", fake_sync):
        scheduler._sync_us_indices()

    assert captured["markets"] == ["us"]
    run = db_session.scalar(
        select(SyncRun).where(SyncRun.job_name == "us_indices")
    )
    assert run is not None
    assert run.status == "success"
    assert run.total == 2


def test_sync_stock_daily_uses_configured_batch_size(db_session) -> None:
    """stock_daily 任务不显式截断头部 200 只，走服务的断点续传默认批。"""
    from app.models.sync_run import SyncRun
    from app.services import scheduler
    from sqlalchemy import select

    captured: dict = {}

    def fake_daily(db, codes=None, **kwargs) -> dict:
        captured["codes"] = codes
        captured["kwargs"] = kwargs
        return {"task": "daily", "status": "success", "total": 0,
                "updated": 0, "failed": 0, "errors": []}

    with patch("app.services.scheduler.SessionLocal", return_value=db_session), \
         patch("app.services.research.stock_data.sync_stock_daily", fake_daily):
        scheduler._sync_stock_daily()

    # 不传 codes / 不传 limit：由 sync_stock_daily 按 research_sync_batch_size 断点选批
    assert captured["codes"] is None
    assert "limit" not in captured["kwargs"]
    run = db_session.scalar(select(SyncRun).where(SyncRun.job_name == "stock_daily"))
    assert run is not None and run.status == "partial"  # 空批次 -> partial 而非 success


def test_main_loop_calls_us_indices_job() -> None:
    """调度主循环里 us_indices 触发独立任务函数（不与 indices 混用）。"""
    import inspect
    from app.services import scheduler

    source = inspect.getsource(scheduler.main)
    assert "_sync_us_indices()" in source
    # 美股时段不再复用 A/港指数任务
    us_block = source.split("next_us_indices", 1)[1]
    assert "_sync_indices()" not in us_block.split("next_holdings")[0]
