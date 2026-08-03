"""全量研究数据可恢复补全编排任务。

该任务把各个已经具备断点/幂等语义的同步服务串成一个长期后台任务：
- A股 raw/qfq 日线、财务、披露日、估值、名称/ST、行业；
- active 基金净值（直接写 DuckDB）、基金画像；
- 最新核心池多年度重仓股与行业配置。

任一批次失败只记录并继续；整个进程中断后可直接重跑，选批均基于实际落库
覆盖率，不会从头重复已完成对象。历史指数调样事件没有可靠自动公开源，留在
人工 CSV 导入流程，不在本任务中猜测或伪造。
"""

from __future__ import annotations

import argparse
import fcntl
import json
import logging
import time
from collections.abc import Iterable
from datetime import date
from pathlib import Path
from typing import Any

from sqlalchemy import select

from app.config import get_settings
from app.db.session import SessionLocal
from app.main import create_tables
from app.models import (
    FundCatalogEntry,
    FundProfile,
    FundWarehouseSyncState,
    StockDailyBar,
    StockFinancialIndicator,
    StockIndustry,
    StockMaster,
    StockNameHistory,
    StockValuation,
)
from app.research.repository import DuckDBRepository
from app.research.warehouse import ResearchWarehouse
from app.services import fund_holdings_backfill, fund_profile_backfill_job
from app.services.research import fund_warehouse_backfill
from app.services.research import stock_data, stock_fundamentals

logger = logging.getLogger(__name__)

DEFAULT_STOCK_BATCH = 50
DEFAULT_FUND_BATCH = 50
DEFAULT_PROFILE_INTERVAL = 0.8


def _chunks(values: list[str], size: int) -> Iterable[list[str]]:
    size = max(size, 1)
    for start in range(0, len(values), size):
        yield values[start : start + size]


def _log_result(phase: str, index: int, total: int, result: dict[str, Any]) -> None:
    payload = json.dumps(result, ensure_ascii=False, default=str)
    print(f"[{phase} {index}/{total}] {payload}", flush=True)


def _missing_codes(db, model, code_column) -> list[str]:
    existing = select(code_column)
    return list(
        db.scalars(
            select(StockMaster.code)
            .where(~StockMaster.code.in_(existing))
            .order_by(StockMaster.code)
        ).all()
    )


def run_stock_phases(stock_batch: int) -> dict[str, Any]:
    """按当前缺失快照依次补齐股票数据域。"""
    db = SessionLocal()
    summary: dict[str, Any] = {}
    try:
        daily_missing = list(
            db.scalars(
                select(StockMaster.code)
                .outerjoin(StockDailyBar, StockDailyBar.code == StockMaster.code)
                .where((StockDailyBar.code.is_(None)) | (StockDailyBar.last_error.is_not(None)))
                .order_by(StockMaster.code)
            ).all()
        )
        batches = list(_chunks(daily_missing, stock_batch))
        for index, codes in enumerate(batches, start=1):
            _log_result(
                "stock_daily",
                index,
                len(batches),
                stock_data.sync_stock_daily(db, codes, fetch_qfq=True),
            )
        summary["stock_daily"] = len(daily_missing)

        financial_missing = _missing_codes(db, StockFinancialIndicator, StockFinancialIndicator.code)
        batches = list(_chunks(financial_missing, stock_batch))
        for index, codes in enumerate(batches, start=1):
            _log_result(
                "financial",
                index,
                len(batches),
                stock_fundamentals.sync_financial_indicators(db, codes),
            )
        summary["financial"] = len(financial_missing)

        valuation_missing = _missing_codes(db, StockValuation, StockValuation.code)
        batches = list(_chunks(valuation_missing, stock_batch))
        for index, codes in enumerate(batches, start=1):
            # 规则价值因子核心只依赖 PE/PB；总市值用于后续行业/规模约束。
            _log_result(
                "valuation",
                index,
                len(batches),
                stock_fundamentals.sync_valuations(
                    db,
                    codes,
                    indicators=["总市值", "市盈率(TTM)", "市净率"],
                    period="近五年",
                ),
            )
        summary["valuation"] = len(valuation_missing)

        names_missing = _missing_codes(db, StockNameHistory, StockNameHistory.code)
        batches = list(_chunks(names_missing, stock_batch))
        for index, codes in enumerate(batches, start=1):
            _log_result(
                "name_history",
                index,
                len(batches),
                stock_fundamentals.sync_name_history(db, codes),
            )
        summary["name_history"] = len(names_missing)

        industry_missing = _missing_codes(db, StockIndustry, StockIndustry.code)
        batches = list(_chunks(industry_missing, stock_batch))
        for index, codes in enumerate(batches, start=1):
            _log_result(
                "industry",
                index,
                len(batches),
                stock_fundamentals.sync_industries(db, codes),
            )
        summary["industry"] = len(industry_missing)

        periods: list[str] = []
        current = date.today()
        for year in range(max(2021, current.year - 5), current.year + 1):
            for suffix, month in (("一季", 3), ("半年报", 6), ("三季", 9), ("年报", 12)):
                if year < current.year or month <= current.month:
                    periods.append(f"{year}{suffix}")
        summary["disclosure"] = stock_fundamentals.sync_report_disclosure(
            db, None, periods
        )
        _log_result("disclosure", 1, 1, summary["disclosure"])
    finally:
        db.close()
    return summary


def run_fund_phases(fund_batch: int, profile_interval: float) -> dict[str, Any]:
    """补 active 基金研究仓净值/画像与最新池历年披露。"""
    settings = get_settings()
    db = SessionLocal()
    summary: dict[str, Any] = {}
    try:
        completed = select(FundWarehouseSyncState.code).where(
            FundWarehouseSyncState.status == "complete"
        )
        pending_nav = list(
            db.scalars(
                select(FundCatalogEntry.code)
                .where(FundCatalogEntry.active.is_(True), ~FundCatalogEntry.code.in_(completed))
                .order_by(FundCatalogEntry.code)
            ).all()
        )
        warehouse = ResearchWarehouse(settings.research_db, settings.research_data_dir)
        repo = DuckDBRepository(warehouse)
        try:
            batches = list(_chunks(pending_nav, fund_batch))
            for index, codes in enumerate(batches, start=1):
                result = fund_warehouse_backfill.run_backfill(
                    db,
                    repo,
                    codes=codes,
                    limit=0,
                    resume=True,
                    dry_run=False,
                    pause_seconds=0.0,
                )
                _log_result("fund_nav_warehouse", index, len(batches), result)
        finally:
            warehouse.close()
        summary["fund_nav_warehouse"] = len(pending_nav)

        valid_profiles = select(FundProfile.code).where(FundProfile.last_error.is_(None))
        pending_profiles = list(
            db.scalars(
                select(FundCatalogEntry.code)
                .where(FundCatalogEntry.active.is_(True), ~FundCatalogEntry.code.in_(valid_profiles))
                .order_by(FundCatalogEntry.code)
            ).all()
        )
        batches = list(_chunks(pending_profiles, fund_batch))
        for index, codes in enumerate(batches, start=1):
            batch, _total = fund_profile_backfill_job.select_batch(
                db, limit=len(codes), codes=codes, force=False
            )
            result = fund_profile_backfill_job.run_batch(
                db, batch, interval=profile_interval, verbose=False
            )
            _log_result("fund_profiles", index, len(batches), result)
        summary["fund_profiles"] = len(pending_profiles)

        pool_id = fund_holdings_backfill.latest_pool_id(db)
        if pool_id is not None:
            for year in range(max(2021, date.today().year - 5), date.today().year + 1):
                pending = fund_holdings_backfill.select_pending(db, year, pool_id=pool_id)
                batches = [pending[i : i + fund_batch] for i in range(0, len(pending), fund_batch)]
                for index, instruments in enumerate(batches, start=1):
                    result = fund_holdings_backfill.backfill_fund_holdings(
                        db,
                        year,
                        pool_id=pool_id,
                        codes=[instrument.code for instrument in instruments],
                        limit=len(instruments),
                        sleep_seconds=0.15,
                    )
                    _log_result(f"fund_holdings_{year}", index, len(batches), result)
                summary[f"fund_holdings_{year}"] = len(pending)
    finally:
        db.close()
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="全量研究数据可恢复补全任务")
    parser.add_argument(
        "--phases",
        default="stock,fund",
        help="逗号分隔：stock,fund（默认全部）",
    )
    parser.add_argument("--stock-batch", type=int, default=DEFAULT_STOCK_BATCH)
    parser.add_argument("--fund-batch", type=int, default=DEFAULT_FUND_BATCH)
    parser.add_argument("--profile-interval", type=float, default=DEFAULT_PROFILE_INTERVAL)
    parser.add_argument(
        "--lock-file",
        default="/root/Src/money/data/full-backfill.lock",
        help="防重复运行锁文件",
    )
    args = parser.parse_args(argv)

    create_tables()
    lock_path = Path(args.lock_file)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock = lock_path.open("a+")
    try:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print("已有全量补数任务在运行，本次退出", flush=True)
        return 2

    phases = {item.strip() for item in args.phases.split(",") if item.strip()}
    result: dict[str, Any] = {}
    try:
        if "stock" in phases:
            result["stock"] = run_stock_phases(args.stock_batch)
        if "fund" in phases:
            result["fund"] = run_fund_phases(args.fund_batch, args.profile_interval)
        print("全量补数任务完成：" + json.dumps(result, ensure_ascii=False, default=str), flush=True)
        return 0
    except Exception:
        logger.exception("全量补数任务异常；已完成批次均已落库，可直接重跑续传")
        return 1
    finally:
        fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
        lock.close()


if __name__ == "__main__":  # pragma: no cover
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    raise SystemExit(main())
