"""快照元数据统一处理。

所有研究数据集共享 5 个元数据列，保证可复现（as-of）查询与血缘追踪：

- ``effective_date`` (DATE)      业务生效日期（净值日期 / 交易日 / 报告期末）；
- ``available_at``   (TIMESTAMP) 数据在当时世界可知的时间点（防前视偏差的核心）；
- ``ingested_at``    (TIMESTAMP) 落入本仓库的时间（默认 now）；
- ``source``         (VARCHAR)   数据来源标识（如 ``eastmoney`` / ``akshare`` / ``manual``）；
- ``row_hash``       (VARCHAR)   业务列内容的 SHA1，用于幂等去重与变更检测。

约定：同一 ``effective_date`` 可能多次到达（数据修订），as-of 查询应取
``available_at <= as_of`` 中 ``available_at`` 最新的版本（见 repository.as_of_filter_sql）。
"""

from __future__ import annotations

import hashlib
from datetime import date, datetime
from typing import Any

import pandas as pd

SNAPSHOT_FIELDS = ("effective_date", "available_at", "ingested_at", "source", "row_hash")


def compute_row_hash(values: list[Any]) -> str:
    """对一行业务值计算稳定 SHA1（None/NaN 统一为空串，日期 ISO 化）。"""
    parts: list[str] = []
    for value in values:
        if value is None or (isinstance(value, float) and pd.isna(value)):
            parts.append("")
        elif isinstance(value, datetime):
            parts.append(value.isoformat())
        elif isinstance(value, date):
            parts.append(value.isoformat())
        else:
            parts.append(str(value))
    return hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()


def normalize_frame(
    df: pd.DataFrame,
    *,
    business_columns: list[str],
    source: str,
    effective_date_col: str = "effective_date",
    available_at: datetime | pd.Timestamp | None = None,
    ingested_at: datetime | pd.Timestamp | None = None,
) -> pd.DataFrame:
    """为业务 DataFrame 补齐/校验快照元数据列，返回新 DataFrame。

    - ``effective_date`` 必须存在（或由 ``effective_date_col`` 指定列改名而来），转 DATE；
    - ``available_at`` 缺省填 ``ingested_at``（再缺省填当前时间）；
      传入标量则整列广播；传入与 df 等长的序列则逐行填充；
    - ``row_hash`` 若未提供则按业务列计算；
    - 不修改入参，返回列顺序 = 业务列 + 快照列。
    """
    if df.empty:
        out = df.copy()
        for col in SNAPSHOT_FIELDS:
            if col not in out.columns:
                out[col] = pd.Series(dtype="object")
        return out[[*business_columns, *SNAPSHOT_FIELDS]]

    out = df.copy()
    if effective_date_col != "effective_date" and effective_date_col in out.columns:
        out = out.rename(columns={effective_date_col: "effective_date"})
    if "effective_date" not in out.columns:
        msg = "缺少 effective_date 列（或通过 effective_date_col 指定）"
        raise ValueError(msg)

    # 直接按 Python date 归一，避免 pandas ns 时间戳对 2262 年以后的日期溢出。
    def _as_date(value):
        if isinstance(value, datetime):
            return value.date()
        if isinstance(value, date):
            return value
        return date.fromisoformat(str(value)[:10])

    out["effective_date"] = out["effective_date"].map(_as_date)

    if "available_at" not in out.columns:
        out["available_at"] = available_at if available_at is not None else pd.NaT
    elif available_at is not None:
        out["available_at"] = available_at
    out["available_at"] = pd.to_datetime(out["available_at"])

    if "ingested_at" not in out.columns:
        out["ingested_at"] = ingested_at if ingested_at is not None else pd.NaT
    elif ingested_at is not None:
        out["ingested_at"] = ingested_at
    out["ingested_at"] = pd.to_datetime(out["ingested_at"])

    now = pd.Timestamp.now()
    out["ingested_at"] = out["ingested_at"].fillna(now)
    out["available_at"] = out["available_at"].fillna(out["ingested_at"])

    if "source" not in out.columns:
        out["source"] = source
    else:
        out["source"] = out["source"].fillna(source)

    if "row_hash" not in out.columns:
        out["row_hash"] = [
            compute_row_hash(list(row)) for row in out[business_columns].itertuples(index=False)
        ]

    return out[[*business_columns, *SNAPSHOT_FIELDS]]


def as_of_latest_sql(dataset: str, key_columns: list[str]) -> str:
    """生成 as-of 取最新版本的 SQL 片段（QUALIFY row_number）。

    用法：``SELECT ... FROM ({sub}) QUALIFY rn = 1``。调用方负责
    在子查询里加 ``available_at <= :as_of`` 过滤。
    """
    keys = ", ".join(key_columns)
    return (
        f"QUALIFY row_number() OVER (PARTITION BY {keys} "
        f"ORDER BY available_at DESC, ingested_at DESC) = 1"
    )
