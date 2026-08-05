"""Download a resumable StockToday/Tushare snapshot without persisting its token.

The token is read only from ``TUSHARE_TOKEN``. Every successful response is stored as Parquet and every
request is recorded in a token-free JSONL manifest. Existing outputs are
skipped, so restarting the command resumes from the last completed partition.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pandas as pd


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--skill-dir", type=Path, required=True)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--start-date", default="20100101")
    parser.add_argument("--end-date", default=date.today().strftime("%Y%m%d"))
    parser.add_argument("--min-interval", type=float, default=0.75)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--retries", type=int, default=2)
    parser.add_argument("--request-timeout", type=float, default=15.0)
    parser.add_argument(
        "--universe-source",
        choices=("current", "historical"),
        default="current",
        help="current=当前800只；historical=历史指数快照出现过的全部股票",
    )
    parser.add_argument(
        "--datasets",
        default="all",
        help="逗号分隔的逐股接口名；all 表示全部",
    )
    parser.add_argument(
        "--retry-empty",
        action="store_true",
        help="重新请求已有 empty 标记；用于代理偶发错误空响应的补拉",
    )
    return parser.parse_args()


def load_token() -> str:
    token = os.getenv("TUSHARE_TOKEN")
    if token:
        return token.strip()
    raise RuntimeError("必须通过 TUSHARE_TOKEN 安全注入数据源凭据")


def universe_codes(database: Path, source: str = "current") -> list[str]:
    connection = sqlite3.connect(database)
    try:
        table = (
            "stock_universe_snapshots"
            if source == "historical"
            else "index_constituents"
        )
        rows = connection.execute(
            f"""
            SELECT DISTINCT stock_code
            FROM {table}
            WHERE index_code IN ('000300', '000905')
            ORDER BY stock_code
            """
        ).fetchall()
    finally:
        connection.close()
    return [str(row[0]).zfill(6) for row in rows]


def universe_snapshot_dates(
    database: Path, start_date: str, end_date: str
) -> list[str]:
    """历史指数快照日期就是策略月频估值所需的最小完备日期集合。"""
    connection = sqlite3.connect(database)
    try:
        rows = connection.execute(
            """
            SELECT DISTINCT replace(snapshot_date, '-', '')
            FROM stock_universe_snapshots
            WHERE replace(snapshot_date, '-', '') BETWEEN ? AND ?
            ORDER BY snapshot_date
            """,
            (start_date, end_date),
        ).fetchall()
    finally:
        connection.close()
    return [str(row[0]) for row in rows]


def ts_code(code: str) -> str:
    if code.startswith(("5", "6", "9")):
        exchange = "SH"
    elif code.startswith(("8", "4")):
        exchange = "BJ"
    else:
        exchange = "SZ"
    return f"{code}.{exchange}"


class Downloader:
    def __init__(
        self,
        client: Any,
        output_dir: Path,
        min_interval: float,
        retries: int,
        retry_empty: bool = False,
    ) -> None:
        self.client = client
        self.output_dir = output_dir
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.manifest = self.output_dir / "manifest.jsonl"
        self.min_interval = max(min_interval, 0.0)
        self.retries = max(retries, 0)
        self.retry_empty = retry_empty
        self.last_call_at = 0.0
        self.rate_lock = threading.Lock()
        self.state_lock = threading.Lock()
        self.manifest_lock = threading.Lock()
        self.calls = 0
        self.rows = 0
        self.failures = 0

    def _wait(self) -> None:
        # 多线程共享一个全局限速器：请求起始时间至少间隔 min_interval。
        with self.rate_lock:
            remaining = self.min_interval - (
                time.monotonic() - self.last_call_at
            )
            if remaining > 0:
                time.sleep(remaining)
            self.last_call_at = time.monotonic()

    def _record(self, item: dict[str, Any]) -> None:
        with self.manifest_lock:
            with self.manifest.open("a", encoding="utf-8") as handle:
                handle.write(
                    json.dumps(item, ensure_ascii=False, default=str) + "\n"
                )

    def fetch(
        self,
        interface: str,
        params: dict[str, Any],
        relative_path: Path,
        call: Callable[[], pd.DataFrame],
    ) -> str:
        target = self.output_dir / relative_path
        empty_marker = target.with_suffix(".empty.json")
        retry_empty_partition = self.retry_empty and (
            "stocks" in relative_path.parts
            or "daily_basic_monthly" in relative_path.parts
        )
        if target.exists():
            return "skipped"
        if empty_marker.exists():
            if not retry_empty_partition:
                return "skipped"
            empty_marker.unlink()
        target.parent.mkdir(parents=True, exist_ok=True)
        started = datetime.now(UTC)
        last_error: Exception | None = None
        for attempt in range(1, self.retries + 2):
            request_counted = False
            try:
                self._wait()
                frame = call()
                with self.state_lock:
                    self.calls += 1
                request_counted = True
                if not isinstance(frame, pd.DataFrame):
                    raise TypeError(
                        f"unexpected response type: {type(frame)!r}"
                    )
                if (
                    frame.empty
                    and retry_empty_partition
                    and attempt <= self.retries
                ):
                    raise RuntimeError(
                        "代理返回空表，按 --retry-empty 继续重试"
                    )
                metadata = {
                    "interface": interface,
                    "params": params,
                    "pulled_at": started.isoformat(),
                    "rows": len(frame),
                    "fields": list(frame.columns),
                    "status": "empty" if frame.empty else "success",
                    "path": str(
                        relative_path
                        if not frame.empty
                        else empty_marker.relative_to(self.output_dir)
                    ),
                    "attempts": attempt,
                }
                if frame.empty:
                    empty_marker.write_text(
                        json.dumps(metadata, ensure_ascii=False, indent=2),
                        encoding="utf-8",
                    )
                else:
                    temp = target.with_suffix(".parquet.tmp")
                    frame.to_parquet(temp, index=False)
                    os.replace(temp, target)
                    with self.state_lock:
                        self.rows += len(frame)
                self._record(metadata)
                return metadata["status"]
            except Exception as exc:
                last_error = exc
                if not request_counted:
                    with self.state_lock:
                        self.calls += 1
                if attempt <= self.retries:
                    time.sleep(min(2**attempt, 8))
                    continue

        if last_error is not None:
            with self.state_lock:
                self.failures += 1
            self._record(
                {
                    "interface": interface,
                    "params": params,
                    "pulled_at": started.isoformat(),
                    "status": "failed",
                    "error": str(last_error)[:500],
                    "path": str(relative_path),
                    "attempts": self.retries + 1,
                }
            )
        return "failed"


def main() -> None:
    args = parse_args()
    load_token()
    scripts = (args.skill_dir / "scripts").resolve()
    sys.path.insert(0, str(scripts))
    from proxy_demo import get_client  # type: ignore[import-not-found]

    client = get_client()
    # Tushare SDK 缺省30秒；历史退市代码偶有网关慢响应，允许短超时后记录
    # failed 并继续其他分区，断点续传时再补，不让一个代码阻塞整个会员窗口。
    client._DataApi__timeout = max(args.request_timeout, 1.0)
    downloader = Downloader(
        client,
        args.output_dir.resolve(),
        args.min_interval,
        args.retries,
        args.retry_empty,
    )
    codes = universe_codes(args.database.resolve(), args.universe_source)
    if len(codes) < 800:
        raise RuntimeError(f"expected at least 800 index constituents, got {len(codes)}")
    requested = (
        None
        if args.datasets == "all"
        else {
            item.strip() for item in args.datasets.split(",") if item.strip()
        }
    )

    # Small global datasets and current metadata.
    stock_basic_fields = ",".join(
        (
            "ts_code",
            "symbol",
            "name",
            "area",
            "industry",
            "fullname",
            "market",
            "exchange",
            "list_status",
            "list_date",
            "delist_date",
            "is_hs",
            "act_name",
            "act_ent_type",
        )
    )
    for status in ("L", "D", "P"):
        params = {"exchange": "", "list_status": status}
        downloader.fetch(
            "stock_basic",
            params,
            Path("global/stock_basic") / f"{status}.parquet",
            lambda status=status: downloader.client.stock_basic(
                exchange="", list_status=status
            ),
        )
        full_params = {**params, "fields": stock_basic_fields}
        downloader.fetch(
            "stock_basic",
            full_params,
            Path("global/stock_basic_full") / f"{status}.parquet",
            lambda full_params=full_params: downloader.client.stock_basic(
                **full_params
            ),
        )
    params = {
        "exchange": "SSE",
        "start_date": "20000101",
        "end_date": "20271231",
    }
    downloader.fetch(
        "trade_cal",
        params,
        Path("global/trade_cal/SSE.parquet"),
        lambda: downloader.client.trade_cal(**params),
    )
    params = {"start_date": "19900101", "end_date": args.end_date}
    downloader.fetch(
        "namechange",
        params,
        Path("global/namechange/all.parquet"),
        lambda: downloader.client.namechange(**params),
    )
    # 逐股 daily_basic 在部分代理节点会对有效股票错误返回空表；按历史指数
    # 月末快照日再拉一次全市场横截面，既补齐缺口，也与月频信号口径完全对齐。
    if requested is None or "daily_basic_monthly" in requested:
        snapshot_dates = universe_snapshot_dates(
            args.database.resolve(), args.start_date, args.end_date
        )
        for index, trade_date in enumerate(snapshot_dates, start=1):
            params = {"trade_date": trade_date}
            downloader.fetch(
                "daily_basic",
                params,
                Path("global/daily_basic_monthly") / f"{trade_date}.parquet",
                lambda params=params: downloader.client.daily_basic(**params),
            )
            if index % 25 == 0 or index == len(snapshot_dates):
                print(
                    json.dumps(
                        {
                            "phase": "daily_basic_monthly",
                            "processed": index,
                            "total": len(snapshot_dates),
                            "calls": downloader.calls,
                            "rows": downloader.rows,
                        },
                        ensure_ascii=False,
                    ),
                    flush=True,
                )
    for index_code in ("000300.SH", "000905.SH"):
        params = {
            "ts_code": index_code,
            "start_date": args.start_date,
            "end_date": args.end_date,
        }
        downloader.fetch(
            "index_daily",
            params,
            Path("indices/index_daily") / f"{index_code}.parquet",
            lambda params=params: downloader.client.index_daily(**params),
        )
        for year in range(int(args.start_date[:4]), int(args.end_date[:4]) + 1):
            params = {
                "index_code": index_code,
                "start_date": f"{year}0101",
                "end_date": f"{year}1231",
            }
            downloader.fetch(
                "index_weight",
                params,
                Path("indices/index_weight") / index_code / f"{year}.parquet",
                lambda params=params: downloader.client.index_weight(**params),
            )

    # 行业分类补充源：申万三级行业用于稳定的行业中性分组，同花顺行业目录
    # 作为交叉核验。index_member_all 的 l1_code 在当前网关会被忽略，因此
    # 成员关系按 800 只目标股票逐股请求，确保不受单次 3000 行上限截断。
    for level in ("L1", "L2", "L3"):
        params = {"level": level, "src": "SW2021"}
        downloader.fetch(
            "index_classify",
            params,
            Path("industries/sw2021") / f"{level}.parquet",
            lambda params=params: downloader.client.index_classify(**params),
        )
    params = {"exchange": "A", "type": "I"}
    downloader.fetch(
        "ths_index",
        params,
        Path("industries/ths/index.parquet"),
        lambda: downloader.client.ths_index(**params),
    )

    endpoint_specs: list[
        tuple[str, Callable[[str], dict[str, Any]]]
    ] = [
        (
            "daily_basic",
            lambda code: {
                "ts_code": code,
                "start_date": args.start_date,
                "end_date": args.end_date,
            },
        ),
        (
            "adj_factor",
            lambda code: {
                "ts_code": code,
                "start_date": args.start_date,
                "end_date": args.end_date,
            },
        ),
        (
            "stk_limit",
            lambda code: {
                "ts_code": code,
                "start_date": args.start_date,
                "end_date": args.end_date,
            },
        ),
        (
            "suspend_d",
            lambda code: {
                "ts_code": code,
                "start_date": args.start_date,
                "end_date": args.end_date,
            },
        ),
        (
            "namechange",
            lambda code: {
                "ts_code": code,
                "start_date": args.start_date,
                "end_date": args.end_date,
            },
        ),
        (
            "index_member_all",
            lambda code: {
                "ts_code": code,
                "is_new": "Y",
            },
        ),
        (
            "fina_indicator",
            lambda code: {
                "ts_code": code,
                "start_date": args.start_date,
                "end_date": args.end_date,
            },
        ),
        (
            "daily",
            lambda code: {
                "ts_code": code,
                "start_date": args.start_date,
                "end_date": args.end_date,
            },
        ),
        (
            "income",
            lambda code: {
                "ts_code": code,
                "start_date": args.start_date,
                "end_date": args.end_date,
            },
        ),
        (
            "balancesheet",
            lambda code: {
                "ts_code": code,
                "start_date": args.start_date,
                "end_date": args.end_date,
            },
        ),
        (
            "cashflow",
            lambda code: {
                "ts_code": code,
                "start_date": args.start_date,
                "end_date": args.end_date,
            },
        ),
        (
            "dividend",
            lambda code: {"ts_code": code},
        ),
    ]
    if requested is not None:
        known = {
            interface for interface, _make_params in endpoint_specs
        } | {"daily_basic_monthly"}
        wanted = requested
        unknown = wanted - known
        if unknown:
            raise RuntimeError(f"unknown datasets: {','.join(sorted(unknown))}")
        endpoint_specs = [
            item
            for item in endpoint_specs
            if item[0] in wanted and item[0] != "daily_basic_monthly"
        ]
    for interface, make_params in endpoint_specs:
        completed = 0
        failed = 0

        def fetch_code(code: str) -> str:
            qualified = ts_code(code)
            params = make_params(qualified)
            return downloader.fetch(
                interface,
                params,
                Path("stocks") / interface / f"{qualified}.parquet",
                lambda interface=interface, params=params: getattr(
                    downloader.client, interface
                )(**params),
            )

        workers = max(1, min(args.workers, len(codes)))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            results = pool.map(fetch_code, codes)
            for index, result in enumerate(results, start=1):
                completed += result in {"success", "empty", "skipped"}
                failed += result == "failed"
                if index % 25 == 0 or index == len(codes):
                    print(
                        json.dumps(
                            {
                                "phase": interface,
                                "processed": index,
                                "total": len(codes),
                                "completed": completed,
                                "failed": failed,
                                "calls": downloader.calls,
                                "rows": downloader.rows,
                            },
                            ensure_ascii=False,
                        ),
                        flush=True,
                    )

    summary = {
        "finished_at": datetime.now(UTC).isoformat(),
        "universe": len(codes),
        "calls": downloader.calls,
        "rows": downloader.rows,
        "failures": downloader.failures,
        "start_date": args.start_date,
        "end_date": args.end_date,
    }
    (downloader.output_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(json.dumps({"done": summary}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
