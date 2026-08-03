"""raw 日线 Parquet 数据湖。

目录布局（root 默认取 settings.research_data_dir）：

    <root>/daily/raw/<code>.parquet   # 不复权 OHLCV，全量历史
    <root>/daily/qfq/<code>.parquet   # 前复权 OHLC，最近一段（受新浪 qfq 接口限制）

写入策略：
- raw：读旧文件 -> 与新数据按 (code, trade_date) 合并去重（新数据优先）-> 原子替换；
- qfq：前复权价随除权事件整体变化，无法增量拼接，每次全量重写；
- 读取失败/文件损坏时返回 None，由调用方决定重抓，绝不伪造数据。
"""

from __future__ import annotations

import logging
import os
from datetime import date
from pathlib import Path

import pandas as pd

from app.config import get_settings

logger = logging.getLogger(__name__)

DAILY_RAW = "raw"
DAILY_QFQ = "qfq"


def data_root(root: Path | None = None) -> Path:
    """返回数据湖根目录（不存在则创建）。"""
    base = root if root is not None else Path(get_settings().research_data_dir)
    base.mkdir(parents=True, exist_ok=True)
    return base


def daily_path(code: str, layer: str = DAILY_RAW, root: Path | None = None) -> Path:
    """单只股票日线 Parquet 路径。"""
    base = data_root(root) / "daily" / layer
    base.mkdir(parents=True, exist_ok=True)
    return base / f"{code}.parquet"


def read_daily(code: str, layer: str = DAILY_RAW, root: Path | None = None) -> pd.DataFrame | None:
    """读取单只股票日线；文件不存在或损坏返回 None。"""
    path = daily_path(code, layer, root)
    if not path.exists():
        return None
    try:
        frame = pd.read_parquet(path)
    except Exception as exc:  # 损坏/版本不兼容，降级为全量重抓
        logger.warning("读取 %s 失败：%s，将全量重建", path, exc)
        return None
    if frame.empty:
        return None
    return frame


def write_daily(
    code: str,
    frame: pd.DataFrame,
    layer: str = DAILY_RAW,
    root: Path | None = None,
    incremental: bool = True,
) -> int:
    """写入单只股票日线，返回落盘行数。

    incremental=True 时与已有文件按 trade_date 合并去重（新行优先），
    否则整表覆盖（qfq 应使用覆盖）。
    frame 必须包含 trade_date 列（date 类型）。
    """
    path = daily_path(code, layer, root)
    frame = frame.dropna(subset=["trade_date"]).drop_duplicates(
        subset=["trade_date"], keep="last"
    )
    if incremental:
        old = read_daily(code, layer, root)
        if old is not None:
            frame = (
                pd.concat([old, frame], ignore_index=True)
                .drop_duplicates(subset=["trade_date"], keep="last")
            )
    frame = frame.sort_values("trade_date").reset_index(drop=True)
    tmp_path = path.with_suffix(".parquet.tmp")
    frame.to_parquet(tmp_path, index=False)
    os.replace(tmp_path, path)  # 原子替换，避免读方看到半成品
    return len(frame)


def daily_coverage(
    code: str, layer: str = DAILY_RAW, root: Path | None = None
) -> tuple[date | None, date | None, int]:
    """返回 (first_trade_date, last_trade_date, rows)；无数据为 (None, None, 0)。"""
    frame = read_daily(code, layer, root)
    if frame is None:
        return None, None, 0
    dates = pd.to_datetime(frame["trade_date"]).dt.date
    return min(dates), max(dates), len(frame)


def list_synced_codes(layer: str = DAILY_RAW, root: Path | None = None) -> list[str]:
    """列出数据湖中已有日线文件的股票代码。"""
    base = data_root(root) / "daily" / layer
    if not base.exists():
        return []
    return sorted(path.stem for path in base.glob("*.parquet"))
