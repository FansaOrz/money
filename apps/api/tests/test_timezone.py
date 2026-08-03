"""北京时间工具模块测试。"""

from datetime import UTC, datetime, timedelta

from app.timezone import CN_TZ, now_cn, to_cn


def test_now_cn_is_aware_with_shanghai_tz() -> None:
    now = now_cn()
    assert now.tzinfo is not None
    assert now.tzname() in ("CST", "+08:00")
    assert now.utcoffset() == timedelta(hours=8)


def test_now_cn_matches_utc_plus_8() -> None:
    cn = now_cn()
    utc = datetime.now(UTC)
    # 两者相差应接近 8 小时（允许秒级误差）
    diff = cn.replace(tzinfo=None) - utc.replace(tzinfo=None)
    assert timedelta(hours=7, minutes=59) < diff < timedelta(hours=8, minutes=1)


def test_to_cn_from_aware_utc() -> None:
    utc_dt = datetime(2026, 7, 31, 4, 0, 0, tzinfo=UTC)
    cn_dt = to_cn(utc_dt)
    assert cn_dt.tzinfo is not None
    assert cn_dt.hour == 12
    assert cn_dt.day == 31
    assert cn_dt.utcoffset() == timedelta(hours=8)


def test_to_cn_from_naive_treated_as_utc() -> None:
    naive = datetime(2026, 1, 1, 0, 0, 0)
    cn_dt = to_cn(naive)
    assert cn_dt.hour == 8
    assert cn_dt.tzinfo is CN_TZ or str(cn_dt.tzinfo) == "Asia/Shanghai"


def test_to_cn_idempotent_for_cn_datetime() -> None:
    cn_dt = now_cn()
    again = to_cn(cn_dt)
    assert again == cn_dt
