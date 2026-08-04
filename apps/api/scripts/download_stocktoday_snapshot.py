"""Download a resumable StockToday/Tushare snapshot without persisting its token.

The token is read from a small Python file (an assignment named ``TOKEN``) or
``TUSHARE_TOKEN``. Every successful response is stored as Parquet and every
request is recorded in a token-free JSONL manifest. Existing outputs are
skipped, so restarting the command resumes from the last completed partition.
"""

from __future__ import annotations

import argparse
import ast
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
    parser.add_argument("--token-file", type=Path)
    parser.add_argument("--skill-dir", type=Path, required=True)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--start-date", default="20100101")
    parser.add_argument("--end-date", default=date.today().strftime("%Y%m%d"))
    parser.add_argument("--min-interval", type=float, default=0.75)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--retries", type=int, default=2)
    return parser.parse_args()


def load_token(path: Path | None) -> str:
    token = os.getenv("TUSHARE_TOKEN")
    if token:
        return token.strip()
    if path is None:
        raise RuntimeError("TUSHARE_TOKEN or --token-file is required")
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if any(isinstance(target, ast.Name) and target.id == "TOKEN" for target in node.targets):
            value = ast.literal_eval(node.value)
            if isinstance(value, str) and value.strip():
                return value.strip()
    raise RuntimeError(f"TOKEN assignment not found in {path}")


def universe_codes(database: Path) -> list[str]:
    connection = sqlite3.connect(database)
    try:
        rows = connection.execute(
            """
            SELECT DISTINCT stock_code
            FROM index_constituents
            WHERE index_code IN ('000300', '000905')
            ORDER BY stock_code
            """
        ).fetchall()
    finally:
        connection.close()
    return [str(row[0]).zfill(6) for row in rows]


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
    ) -> None:
        self.client = client
        self.output_dir = output_dir
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.manifest = self.output_dir / "manifest.jsonl"
        self.min_interval = max(min_interval, 0.0)
        self.retries = max(retries, 0)
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
        if target.exists() or empty_marker.exists():
            return "skipped"
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
    token = load_token(args.token_file)
    scripts = (args.skill_dir / "scripts").resolve()
    sys.path.insert(0, str(scripts))
    from proxy_demo import get_client  # type: ignore[import-not-found]

    downloader = Downloader(
        get_client(token=token),
        args.output_dir.resolve(),
        args.min_interval,
        args.retries,
    )
    codes = universe_codes(args.database.resolve())
    if len(codes) != 800:
        raise RuntimeError(f"expected 800 index constituents, got {len(codes)}")

    # Small global datasets and current metadata.
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
