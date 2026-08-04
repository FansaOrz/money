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
    assert (times["fund_nav_early"].hour, times["fund_nav_early"].minute) == (19, 30)
    nav = times["fund_nav"]
    assert (nav.hour, nav.minute) == (20, 30)
    assert nav.day == 15
    assert (times["fund_nav_late"].hour, times["fund_nav_late"].minute) == (22, 0)
    # paper 等最后一轮净值同步完成后执行，覆盖晚披露基金
    assert times["paper"] == times["fund_nav_late"]


def test_nav_schedule_rolls_to_next_day_after_deadline() -> None:
    times = next_run_times(_at(21, 0))
    nav = times["fund_nav"]
    assert (nav.hour, nav.minute) == (20, 30)
    assert nav.day == 16
    # 22:00 补抓尚未发生，仍应安排在当天。
    assert times["fund_nav_late"].day == 15


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


def test_stock_reference_schedule_precedes_market_close() -> None:
    times = next_run_times(_at(10, 0))
    reference = times["stock_reference"]
    daily = times["stock_daily"]
    assert (reference.hour, reference.minute) == (16, 10)
    assert reference < daily


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


def test_sync_stock_daily_uses_small_scheduled_batch(db_session) -> None:
    """调度器直调入口使用独立的小批次配置，避免单轮运行数小时。"""
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

    # 不传 codes；limit 使用调度器专用小批次，服务仍按断点选择标的。
    assert captured["codes"] is None
    assert captured["kwargs"]["limit"] == 40
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


def test_main_loop_launches_stock_daily_in_background() -> None:
    """A 股长任务只能启动后台看护，不能在主循环同步执行。"""
    import inspect
    from app.services import scheduler

    source = inspect.getsource(scheduler.main)
    stock_block = source.split("if now >= next_stock_daily:", 1)[1].split(
        "if now >= next_fund_catalog:", 1
    )[0]
    assert "_launch_stock_daily()" in stock_block
    assert "_sync_stock_daily()" not in stock_block
