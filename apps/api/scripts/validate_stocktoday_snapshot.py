"""Validate completeness of the token-free StockToday Parquet snapshot."""

from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pandas as pd
import pyarrow.parquet as pq


STOCK_ENDPOINTS = (
    "daily_basic",
    "adj_factor",
    "stk_limit",
    "suspend_d",
    "namechange",
    "index_member_all",
    "fina_indicator",
    "daily",
    "income",
    "balancesheet",
    "cashflow",
    "dividend",
)
REQUIRED_FIELDS = {
    "daily_basic": {"ts_code", "trade_date", "pe_ttm", "pb"},
    "adj_factor": {"ts_code", "trade_date", "adj_factor"},
    "stk_limit": {"ts_code", "trade_date", "up_limit", "down_limit"},
    "index_member_all": {"ts_code", "l1_code", "l1_name"},
    "fina_indicator": {"ts_code", "end_date"},
    "daily": {"ts_code", "trade_date", "open", "close"},
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--snapshot-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    return parser.parse_args()


def universe(database: Path) -> set[str]:
    connection = sqlite3.connect(database)
    try:
        rows = connection.execute(
            """
            SELECT DISTINCT stock_code
            FROM index_constituents
            WHERE index_code IN ('000300', '000905')
            """
        ).fetchall()
    finally:
        connection.close()
    result: set[str] = set()
    for (raw_code,) in rows:
        code = str(raw_code).zfill(6)
        exchange = "SH" if code.startswith(("5", "6", "9")) else "SZ"
        if code.startswith(("4", "8")):
            exchange = "BJ"
        result.add(f"{code}.{exchange}")
    return result


def partition_names(base: Path) -> set[str]:
    parquet = {path.stem for path in base.glob("*.parquet")}
    empty = {
        path.name.removesuffix(".empty.json")
        for path in base.glob("*.empty.json")
    }
    return parquet | empty


def parquet_rows(paths: list[Path]) -> int:
    return sum(pq.ParquetFile(path).metadata.num_rows for path in paths)


def validate_index(
    snapshot_root: Path, index_code: str
) -> dict[str, Any]:
    paths = sorted(
        (snapshot_root / "indices" / "index_weight" / index_code).glob(
            "*.parquet"
        )
    )
    frames = [
        pd.read_parquet(
            path, columns=["index_code", "con_code", "trade_date"]
        )
        for path in paths
    ]
    if not frames:
        return {
            "files": 0,
            "rows": 0,
            "dates": 0,
            "latest_date": None,
            "latest_members": 0,
        }
    frame = pd.concat(frames, ignore_index=True)
    dates = pd.to_datetime(frame["trade_date"], errors="coerce")
    latest = dates.max()
    members = frame.loc[dates == latest, "con_code"].dropna().nunique()
    return {
        "files": len(paths),
        "rows": len(frame),
        "dates": dates.nunique(),
        "earliest_date": dates.min().date().isoformat(),
        "latest_date": latest.date().isoformat(),
        "latest_members": int(members),
    }


def main() -> None:
    args = parse_args()
    database = args.database.resolve()
    snapshot_root = args.snapshot_dir.resolve()
    expected = universe(database)
    endpoints: dict[str, Any] = {}
    missing_total = 0
    schema_errors: list[str] = []
    for endpoint in STOCK_ENDPOINTS:
        base = snapshot_root / "stocks" / endpoint
        names = partition_names(base)
        missing = sorted(expected - names)
        paths = sorted(base.glob("*.parquet"))
        if paths and endpoint in REQUIRED_FIELDS:
            actual_fields = set(pq.ParquetFile(paths[0]).schema.names)
            absent = sorted(REQUIRED_FIELDS[endpoint] - actual_fields)
            if absent:
                schema_errors.append(f"{endpoint}: 缺少字段 {absent}")
        endpoints[endpoint] = {
            "complete_partitions": len(names & expected),
            "parquet_files": len(paths),
            "empty_partitions": len(
                list(base.glob("*.empty.json"))
            ),
            "rows": parquet_rows(paths),
            "missing": missing[:20],
            "missing_count": len(missing),
        }
        missing_total += len(missing)

    indices = {
        code: validate_index(snapshot_root, code)
        for code in ("000300.SH", "000905.SH")
    }
    index_errors = [
        f"{code}: 最新成员数 {item['latest_members']}，预期 {expected_count}"
        for code, item, expected_count in (
            ("000300.SH", indices["000300.SH"], 300),
            ("000905.SH", indices["000905.SH"], 500),
        )
        if item["latest_members"] != expected_count
    ]
    errors = schema_errors + index_errors
    if len(expected) != 800:
        errors.append(f"当前指数并集 {len(expected)}，预期 800")
    if missing_total:
        errors.append(f"逐股分区共缺失 {missing_total}")
    result = {
        "status": "success" if not errors else "failed",
        "validated_at": datetime.now(UTC).isoformat(),
        "universe": len(expected),
        "endpoints": endpoints,
        "indices": indices,
        "errors": errors,
    }
    output = args.output or snapshot_root / "validation.json"
    output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(json.dumps(result, ensure_ascii=False))
    if errors:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
