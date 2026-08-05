"""官方基准与指数权重数据的获取、规范化和可追溯存储。"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import tempfile
import time
from typing import Any, Callable

import requests
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import QuantDataRecord, QuantImportRun

CSINDEX_PERFORMANCE_URL = (
    "https://www.csindex.com.cn/csindex-home/perf/index-perf"
)
OFFICIAL_TOTAL_RETURN_CODES = {"H00906": "中证800全收益指数"}


class BenchmarkDataError(RuntimeError):
    """官方基准数据不完整、不可识别或无法持久化。"""


def transparent_style_benchmarks(
    periods: list[dict[str, dict[str, float]]],
    *,
    styles: tuple[str, ...] = ("low_volatility", "value", "quality"),
    one_way_cost: float = 0.001,
) -> dict[str, list[float]]:
    """用每期当时可得截面构造透明 top-quintile 等权风格净值。

    每期结构为 ``code -> {forward_return, low_volatility, value, quality}``；
    low-volatility 分数越低越好，其余越高越好。换仓按一次完整单边成本保守扣除。
    """
    curves = {style: [1.0] for style in styles}
    for period in periods:
        for style in styles:
            eligible = [
                (code, values)
                for code, values in period.items()
                if style in values and "forward_return" in values
            ]
            reverse = style != "low_volatility"
            eligible.sort(key=lambda item: item[1][style], reverse=reverse)
            count = max(1, len(eligible) // 5) if eligible else 0
            period_return = (
                sum(item[1]["forward_return"] for item in eligible[:count]) / count
                - one_way_cost
                if count
                else 0.0
            )
            curves[style].append(curves[style][-1] * (1 + period_return))
    return curves


def _parse_day(value: object) -> date | None:
    text = str(value or "").replace("-", "")
    if len(text) < 8 or not text[:8].isdigit():
        return None
    try:
        return date(int(text[:4]), int(text[4:6]), int(text[6:8]))
    except ValueError:
        return None


def _atomic_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{path.name}.", dir=path.parent
    )
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _fetch_chunk(
    code: str,
    start: date,
    end: date,
    request_get: Callable[..., Any],
) -> list[object]:
    last_error: Exception | None = None
    for attempt in range(3):
        try:
            response = request_get(
                CSINDEX_PERFORMANCE_URL,
                params={
                    "indexCode": code,
                    "startDate": start.strftime("%Y%m%d"),
                    "endDate": end.strftime("%Y%m%d"),
                },
                timeout=60,
            )
            response.raise_for_status()
            body = response.json()
            rows = body.get("data") if isinstance(body, dict) else None
            if not isinstance(rows, list):
                raise BenchmarkDataError(
                    f"中证指数接口未返回有效 data：{start} 至 {end}"
                )
            return rows
        except (requests.RequestException, ValueError, BenchmarkDataError) as exc:
            last_error = exc
            if attempt < 2:
                time.sleep(attempt + 1)
    raise BenchmarkDataError(
        f"中证指数接口连续失败：{start} 至 {end}：{last_error}"
    )


def sync_csindex_total_return(
    db: Session,
    *,
    code: str,
    start: date,
    end: date,
    data_root: Path,
    request_get: Callable[..., Any] = requests.get,
) -> dict[str, object]:
    """按年获取中证官网全收益指数并写入版本化原始文件和规范化记录。"""
    expected_name = OFFICIAL_TOTAL_RETURN_CODES.get(code)
    if expected_name is None:
        raise BenchmarkDataError(f"未批准的全收益指数代码：{code}")
    if start > end:
        raise BenchmarkDataError("基准起始日期晚于结束日期")

    started_at = datetime.now(UTC)
    run = QuantImportRun(
        dataset="index_total_return",
        status="running",
        source_root=CSINDEX_PERFORMANCE_URL,
        imported=0,
        skipped=0,
        invalid=0,
        detail={"code": code, "start": start.isoformat(), "end": end.isoformat()},
        started_at=started_at,
    )
    db.add(run)
    db.commit()

    try:
        raw_rows: list[object] = []
        for year in range(start.year, end.year + 1):
            chunk_start = max(start, date(year, 1, 1))
            chunk_end = min(end, date(year, 12, 31))
            raw_rows.extend(
                _fetch_chunk(code, chunk_start, chunk_end, request_get)
            )

        normalized: dict[date, Decimal] = {}
        invalid = 0
        for row in raw_rows:
            if isinstance(row, dict):
                raw_day = row.get("tradeDate")
                raw_code = row.get("indexCode")
                raw_name = row.get("indexNameCnAll")
                raw_close = row.get("close")
            elif isinstance(row, list) and len(row) >= 10:
                raw_day, raw_code, raw_name, raw_close = (
                    row[0],
                    row[1],
                    row[2],
                    row[9],
                )
            else:
                invalid += 1
                continue
            day = _parse_day(raw_day)
            row_code = str(raw_code or "")
            row_name = str(raw_name or "")
            try:
                close = Decimal(str(raw_close))
            except Exception:  # noqa: BLE001 - 外部 JSON 类型不可控
                invalid += 1
                continue
            if (
                day is None
                or day < start
                or day > end
                or row_code != code
                or row_name != expected_name
                or close <= 0
            ):
                invalid += 1
                continue
            previous = normalized.get(day)
            if previous is not None and previous != close:
                raise BenchmarkDataError(
                    f"{code} {day} 出现相互冲突的官方收盘点位"
                )
            normalized[day] = close
        if not normalized:
            raise BenchmarkDataError(f"{code} 没有可用官方全收益行情")

        canonical_rows = [
            [day.isoformat(), format(value, "f")]
            for day, value in sorted(normalized.items())
        ]
        canonical = json.dumps(
            {
                "source": CSINDEX_PERFORMANCE_URL,
                "index_code": code,
                "index_name": expected_name,
                "rows": canonical_rows,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        source_hash = hashlib.sha256(canonical).hexdigest()
        relative = Path("benchmarks") / f"{code}.{source_hash}.json"
        destination = data_root / relative
        _atomic_write(destination, canonical)

        source = f"csindex:{code}:{source_hash[:12]}"
        existing = {
            row.effective_date
            for row in db.scalars(
                select(QuantDataRecord).where(
                    QuantDataRecord.dataset == "index_total_return",
                    QuantDataRecord.code == code,
                    QuantDataRecord.source_hash == source_hash,
                )
            ).all()
        }
        imported_at = datetime.now(UTC)
        imported = 0
        skipped = 0
        for day, close in sorted(normalized.items()):
            if day in existing:
                skipped += 1
                continue
            db.add(
                QuantDataRecord(
                    dataset="index_total_return",
                    code=code,
                    effective_date=day,
                    available_at=imported_at,
                    source=source,
                    source_file=str(relative),
                    source_hash=source_hash,
                    payload={
                        "index_name": expected_name,
                        "close": format(close, "f"),
                        "return_kind": "gross_total_return",
                    },
                    imported_at=imported_at,
                )
            )
            imported += 1
        run.status = "success"
        run.imported = imported
        run.skipped = skipped
        run.invalid = invalid
        run.detail = {
            **dict(run.detail or {}),
            "source_hash": source_hash,
            "source_file": str(relative),
            "rows": len(normalized),
            "first_date": min(normalized).isoformat(),
            "last_date": max(normalized).isoformat(),
            "return_kind": "gross_total_return",
        }
        run.finished_at = datetime.now(UTC)
        db.commit()
        return dict(run.detail)
    except Exception as exc:
        run.status = "failed"
        run.detail = {**dict(run.detail or {}), "error": str(exc)}
        run.finished_at = datetime.now(UTC)
        db.commit()
        raise
