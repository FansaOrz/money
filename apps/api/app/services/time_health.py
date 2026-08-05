"""主机时钟、NTP、UTC/北京时间配置与下单安全门禁。"""

from __future__ import annotations

import subprocess
from datetime import datetime
from zoneinfo import ZoneInfo


def time_health(
    *,
    measured_offset_seconds: float | None = None,
    maximum_offset_seconds: float = 1.0,
) -> dict[str, object]:
    offset = measured_offset_seconds
    source = "injected"
    if offset is None:
        source = "chronyc"
        try:
            output = subprocess.run(
                ["chronyc", "tracking"],
                check=True,
                capture_output=True,
                text=True,
                timeout=3,
            ).stdout
            line = next(
                row for row in output.splitlines() if "Last offset" in row
            )
            offset = float(line.split(":", 1)[1].strip().split()[0])
        except (OSError, subprocess.SubprocessError, StopIteration, ValueError):
            source = "unavailable"
            offset = None
    timezone_checks = {
        "utc": datetime.now(ZoneInfo("UTC")).utcoffset().total_seconds() == 0,
        "asia_shanghai": (
            datetime.now(ZoneInfo("Asia/Shanghai")).utcoffset().total_seconds()
            == 8 * 3600
        ),
    }
    synchronized = (
        offset is not None and abs(offset) <= maximum_offset_seconds
    )
    return {
        "source": source,
        "offset_seconds": offset,
        "maximum_offset_seconds": maximum_offset_seconds,
        "timezone_checks": timezone_checks,
        "synchronized": synchronized,
        "allow_live_orders": synchronized and all(timezone_checks.values()),
        "status": "ok" if synchronized else "failed",
    }
