"""全市场基金净值 → 研究仓库（DuckDB warehouse）的可恢复批处理回填。

设计目标（区别于 ``sync_backfill_job`` 的 SQLite 回填）：
- 数据源是 ``fund_catalog`` 的 active 基金（数万只），**不要求**存在
  ``Instrument`` 记录，本任务也绝不创建/修改 ``instruments`` / ``fund_navs``；
- 历史净值不落 SQLite，直接通过 ``DuckDBRepository.write_fund_nav`` 写入
  研究仓库 ``fund_nav`` 数据集（DuckDB 主表 + year=YYYY Parquet 分区）；
- 断点状态记录在独立表 ``fund_warehouse_sync_state``（按 code 唯一），
  选批优先级：never（无状态行）→ failed → 最早 ``earliest_nav_date`` 优先。

口径约定：
- 源数据的 ``daily_growth_rate`` 是**百分数**（如 1.5 表示 +1.5%），
  写入仓库 ``daily_return`` 时除以 100 转为小数（0.015）；
- 历史回填无法还原"当时可见时间"，``available_at`` 一律取写入时刻
  （= ``ingested_at``，由 ``repository.write`` 默认填充），``source``
  明确加 ``backfill_`` 前缀（如 ``backfill_eastmoney_fast``），
  不伪称这些数据在历史时点已经可见；
- 重试与限速完全沿用 ``fund_data.fetch_nav_history_with_fallback``
  （单源失败重试 3 次、请求间隔 0.25s、快速源 → lsjz 分页 → AKShare/港基回退）。

用法（CLI）::

    python -m app.services.research.fund_warehouse_backfill --dry-run --limit 50
    python -m app.services.research.fund_warehouse_backfill --limit 50
    python -m app.services.research.fund_warehouse_backfill --codes 110022,000001
    python -m app.services.research.fund_warehouse_backfill --limit 0   # 全部 active

幂等性：仓库写入按 (fund_code, effective_date) 删除同内容行后重写，
重复执行/中断重跑不会重复计数；已 complete 且 earliest 覆盖目标起点的基金
自动跳过（``--no-resume`` 可强制重写）。
"""

from __future__ import annotations

import argparse
import json
import logging
import time
from datetime import date, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any

import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import FundCatalogEntry, FundWarehouseSyncState
from app.services import fund_data

if TYPE_CHECKING:
    from app.research.repository import DuckDBRepository

logger = logging.getLogger(__name__)

#: 历史回填写入仓库时给来源加的前缀：明确该批 available_at=写入时刻，非历史可见
BACKFILL_SOURCE_PREFIX = "backfill_"

DEFAULT_BATCH_LIMIT = 50
DEFAULT_YEARS = fund_data.DEFAULT_YEARS

_STATUS_COMPLETE = "complete"
_STATUS_FAILED = "failed"


# ---------------------------------------------------------------------------
# 选批（断点优先级）
# ---------------------------------------------------------------------------


def _state_priority_code(entry: FundCatalogEntry, state: FundWarehouseSyncState | None) -> tuple:
    """选批排序键：failed → never → 已写入部分按最早净值日期优先（oldest first）。

    返回 (优先级, 排序键, code)，同优先级内日期越早越靠前，再次按代码保证稳定。
    failed 排在 never 之前：失败重试优先，避免失败基金在 never 队列后长期饥饿。
    """
    if state is not None and state.status == _STATUS_FAILED:
        return (0, state.earliest_nav_date or date.min, entry.code)
    if state is None:
        return (1, date.min, entry.code)
    return (2, state.earliest_nav_date or date.min, entry.code)


def select_batch(
    db: Session,
    *,
    limit: int = DEFAULT_BATCH_LIMIT,
    codes: list[str] | None = None,
) -> list[tuple[FundCatalogEntry, FundWarehouseSyncState | None]]:
    """从 fund_catalog active 基金中按断点优先级选出一批。

    - ``codes`` 显式指定时只在这些基金内按同样优先级排序；
    - 返回 (目录条目, 状态行或 None)；``limit <= 0`` 表示不限。
    """
    entries = db.scalars(
        select(FundCatalogEntry).where(FundCatalogEntry.active.is_(True))
    ).all()
    states = {
        state.code: state
        for state in db.scalars(select(FundWarehouseSyncState)).all()
    }
    pairs = [(entry, states.get(entry.code)) for entry in entries]
    if codes is not None:
        wanted = set(codes)
        pairs = [(entry, state) for entry, state in pairs if entry.code in wanted]
    pairs.sort(key=lambda pair: _state_priority_code(*pair))
    if limit and limit > 0:
        pairs = pairs[:limit]
    return pairs


# ---------------------------------------------------------------------------
# 单只基金回填
# ---------------------------------------------------------------------------


def _rows_to_frame(code: str, rows: list[dict]) -> pd.DataFrame:
    """fetch_nav_history_with_fallback 的行 → fund_nav 业务 DataFrame。

    ``daily_growth_rate``（百分数）/ 100 → ``daily_return``（小数）。
    """

    def _to_float(value: Any) -> float | None:
        if value is None:
            return None
        if isinstance(value, Decimal):
            return float(value)
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    frame = pd.DataFrame(
        {
            "fund_code": [code] * len(rows),
            "effective_date": [row["nav_date"] for row in rows],
            "nav": [_to_float(row.get("unit_nav")) for row in rows],
            "accumulated_nav": [_to_float(row.get("accumulated_nav")) for row in rows],
            "daily_return": [
                (value / 100.0) if (value := _to_float(row.get("daily_growth_rate"))) is not None
                else None
                for row in rows
            ],
        }
    )
    return frame


def _record_state(
    db: Session,
    code: str,
    *,
    status: str,
    target_start: date,
    row_count: int = 0,
    earliest: date | None = None,
    latest: date | None = None,
    source: str | None = None,
    error: str | None = None,
) -> FundWarehouseSyncState:
    """写入/更新单只基金的仓库同步状态并提交。"""
    state = db.scalar(
        select(FundWarehouseSyncState).where(FundWarehouseSyncState.code == code)
    )
    if state is None:
        state = FundWarehouseSyncState(code=code)
        db.add(state)
    state.status = status
    state.target_start_date = target_start
    state.row_count = row_count
    state.earliest_nav_date = earliest
    state.latest_nav_date = latest
    state.last_source = source or state.last_source
    state.last_error = error
    state.last_synced_at = datetime.now()
    db.commit()
    return state


def _warehouse_coverage(
    repo: DuckDBRepository, code: str
) -> tuple[date | None, date | None, int]:
    """查询仓库中该基金的实际覆盖范围（earliest, latest, row_count），以仓库为准。"""
    frame = repo.warehouse.conn.execute(
        "SELECT min(effective_date), max(effective_date), count(*) "
        "FROM fund_nav_all WHERE fund_code = ?",
        [code],
    ).fetchone()
    earliest, latest, row_count = frame[0], frame[1], int(frame[2] or 0)
    if earliest is not None and hasattr(earliest, "date"):
        earliest = earliest.date() if not isinstance(earliest, date) else earliest
    if latest is not None and hasattr(latest, "date"):
        latest = latest.date() if not isinstance(latest, date) else latest
    return earliest, latest, row_count


def backfill_fund(
    db: Session,
    repo: DuckDBRepository,
    code: str,
    *,
    years: int = DEFAULT_YEARS,
    resume: bool = True,
    use_fallback: bool = True,
    timeout: int = 15,
) -> dict[str, Any]:
    """回填单只目录基金的历史净值到研究仓库（不要求 Instrument）。

    返回 {"code", "status", "rows", "source", "error"}：
    - complete：成功写入（或源已无数据、或已覆盖目标起点跳过）；
    - skipped：已 complete 且覆盖目标起点（resume 模式）；
    - failed：所有来源失败，错误记录到状态表。
    """
    years = min(max(years, 1), fund_data.MAX_YEARS)
    _, target_start = fund_data.resolve_window(years=years)
    state = db.scalar(
        select(FundWarehouseSyncState).where(FundWarehouseSyncState.code == code)
    )
    if (
        resume
        and state is not None
        and state.status == _STATUS_COMPLETE
        and state.earliest_nav_date is not None
        and state.earliest_nav_date <= target_start
    ):
        return {"code": code, "status": "skipped", "rows": 0, "source": None, "error": None}

    rows, error, source = fund_data.fetch_nav_history_with_fallback(
        code, years=years, use_fallback=use_fallback, timeout=timeout
    )
    backfill_source = f"{BACKFILL_SOURCE_PREFIX}{source}" if source else None
    if not rows:
        # 已有覆盖达到目标起点时，本轮拿不到数据属于完成而非失败
        # （常见于上次已写满目标窗口，但状态曾被标为 failed）。
        existing_earliest = state.earliest_nav_date if state else None
        if existing_earliest is not None and existing_earliest <= target_start:
            _record_state(
                db,
                code,
                status=_STATUS_COMPLETE,
                target_start=target_start,
                row_count=state.row_count,
                earliest=state.earliest_nav_date,
                latest=state.latest_nav_date,
                source=backfill_source,
                error=None,
            )
            return {
                "code": code,
                "status": _STATUS_COMPLETE,
                "rows": 0,
                "source": backfill_source,
                "error": None,
            }
        _record_state(
            db,
            code,
            status=_STATUS_FAILED,
            target_start=target_start,
            source=backfill_source,
            error=error or "未获取到净值数据",
        )
        return {
            "code": code,
            "status": _STATUS_FAILED,
            "rows": 0,
            "source": backfill_source,
            "error": error,
        }

    frame = _rows_to_frame(code, rows)
    try:
        written = repo.write_fund_nav(frame, source=backfill_source)
    except Exception as exc:  # noqa: BLE001 - 仓库写失败记状态，不中断整批
        logger.exception("基金 %s 写入研究仓库失败", code)
        _record_state(
            db,
            code,
            status=_STATUS_FAILED,
            target_start=target_start,
            source=backfill_source,
            error=f"写入研究仓库失败：{exc}",
        )
        return {
            "code": code,
            "status": _STATUS_FAILED,
            "rows": 0,
            "source": backfill_source,
            "error": str(exc),
        }
    # 覆盖范围以仓库实际数据为准（失败重试后跨源累计的窗口可能大于本批）
    earliest, latest, total_rows = _warehouse_coverage(repo, code)
    _record_state(
        db,
        code,
        status=_STATUS_COMPLETE,
        target_start=target_start,
        row_count=total_rows,
        earliest=earliest,
        latest=latest,
        source=backfill_source,
        error=None,
    )
    return {
        "code": code,
        "status": _STATUS_COMPLETE,
        "rows": written,
        "source": backfill_source,
        "error": None,
    }


# ---------------------------------------------------------------------------
# 批量编排
# ---------------------------------------------------------------------------


def run_backfill(
    db: Session,
    repo: DuckDBRepository,
    *,
    years: int = DEFAULT_YEARS,
    limit: int = DEFAULT_BATCH_LIMIT,
    codes: list[str] | None = None,
    resume: bool = True,
    use_fallback: bool = True,
    dry_run: bool = False,
    pause_seconds: float = 0.0,
    verbose: bool = False,
) -> dict[str, Any]:
    """选批并逐只回填，返回汇总。

    ``dry_run=True`` 只列出将处理的基金与断点状态，不发起请求、不写仓库。
    """
    batch = select_batch(db, limit=limit, codes=codes)
    if dry_run:
        return {
            "dry_run": True,
            "years": years,
            "selected": len(batch),
            "funds": [
                {
                    "code": entry.code,
                    "name": entry.name,
                    "state": state.status if state else "never",
                    "earliest": state.earliest_nav_date.isoformat()
                    if state and state.earliest_nav_date
                    else None,
                    "row_count": state.row_count if state else 0,
                }
                for entry, state in batch
            ],
        }

    results: list[dict[str, Any]] = []
    for index, (entry, _state) in enumerate(batch, start=1):
        result = backfill_fund(
            db,
            repo,
            entry.code,
            years=years,
            resume=resume,
            use_fallback=use_fallback,
        )
        results.append(result)
        if verbose:
            print(
                f"[{index}/{len(batch)}] {result['code']} -> {result['status']} "
                f"写入 {result['rows']} 行 source={result['source']}"
                + (f" 错误：{result['error']}" if result["error"] else "")
            )
        if pause_seconds > 0 and index < len(batch):
            time.sleep(pause_seconds)

    return {
        "dry_run": False,
        "years": years,
        "selected": len(batch),
        "complete": sum(1 for r in results if r["status"] == _STATUS_COMPLETE),
        "skipped": sum(1 for r in results if r["status"] == "skipped"),
        "failed": sum(1 for r in results if r["status"] == _STATUS_FAILED),
        "rows": sum(r["rows"] for r in results),
        "failures": [r for r in results if r["status"] == _STATUS_FAILED],
        "details": results,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _build_repo(db_path: str | None, data_dir: str | None) -> "DuckDBRepository":
    from app.config import get_settings
    from app.research.repository import DuckDBRepository
    from app.research.warehouse import ResearchWarehouse

    settings = get_settings()
    warehouse = ResearchWarehouse(
        db_path or settings.research_db,
        data_dir or settings.research_data_dir,
    )
    return DuckDBRepository(warehouse)


def main(argv: list[str] | None = None) -> int:
    """CLI 入口：``python -m app.services.research.fund_warehouse_backfill``。"""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    parser = argparse.ArgumentParser(
        description="全市场基金（fund_catalog active）净值 → DuckDB 研究仓库，可恢复批处理"
    )
    parser.add_argument("--years", type=int, default=DEFAULT_YEARS, help="回填年限，上限 5 年")
    parser.add_argument(
        "--limit",
        type=int,
        default=DEFAULT_BATCH_LIMIT,
        help=f"本批基金数量（默认 {DEFAULT_BATCH_LIMIT}，0 表示全部 active）",
    )
    parser.add_argument("--codes", type=str, default="", help="逗号分隔的基金代码，仅回填这些基金")
    parser.add_argument("--db", default=None, help="研究仓库 DuckDB 文件（默认 settings.research_db）")
    parser.add_argument("--data-dir", default=None, help="研究仓库 Parquet 根目录（默认 settings.research_data_dir）")
    parser.add_argument("--no-resume", action="store_true", help="忽略断点，重写已 complete 的基金")
    parser.add_argument("--no-fallback", action="store_true", help="禁用 AKShare 回退")
    parser.add_argument("--dry-run", action="store_true", help="只列出将处理的基金，不请求不写入")
    parser.add_argument("--pause", type=float, default=0.0, help="基金之间额外暂停秒数（源内限速已内置）")
    args = parser.parse_args(argv)

    # 显式 --codes 时不叠加 limit 截断（指定了几只就处理几只）
    codes = [c.strip() for c in args.codes.split(",") if c.strip()] or None
    limit = 0 if codes else args.limit

    from app.db.session import SessionLocal
    from app.main import create_tables

    create_tables()  # 确保 fund_warehouse_sync_state 已建表（幂等）
    db = SessionLocal()
    repo = None if args.dry_run else _build_repo(args.db, args.data_dir)
    try:
        summary = run_backfill(
            db,
            repo,
            years=args.years,
            limit=limit,
            codes=codes,
            resume=not args.no_resume,
            use_fallback=not args.no_fallback,
            dry_run=args.dry_run,
            pause_seconds=args.pause,
            verbose=not args.dry_run,
        )
    finally:
        db.close()
        if repo is not None:
            repo.warehouse.close()
    print(json.dumps(summary, ensure_ascii=False, indent=2, default=str))
    return 0 if not summary.get("failed") else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
