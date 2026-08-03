"""北京时间工具模块。

全项目统一使用 Asia/Shanghai（UTC+8，无夏令时）作为业务时区：
调度器的计划时间、日志时间戳、同步运行记录均以此为基准。
"""

from datetime import datetime
from zoneinfo import ZoneInfo

CN_TZ = ZoneInfo("Asia/Shanghai")


def now_cn() -> datetime:
    """返回当前北京时间（aware datetime，tzinfo=Asia/Shanghai）。"""
    return datetime.now(CN_TZ)


def to_cn(dt: datetime) -> datetime:
    """将任意 datetime 转换为北京时间。

    naive datetime 视为 UTC 处理；aware datetime 直接做时区换算。
    """
    if dt.tzinfo is None:
        from datetime import UTC

        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(CN_TZ)
