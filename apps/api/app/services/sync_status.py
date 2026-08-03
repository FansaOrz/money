"""同步运行状态服务：记录每次任务执行并对外提供状态查询。

用法（上下文管理器）::

    with track_sync_run(db, "fund_nav") as record:
        result = do_sync(db)
        record(total=10, updated=8, failed=2)      # 有成功有失败 -> partial
        record(status="paused")                    # 也可显式覆盖状态

- 正常退出时写入终态并落库：record() 未显式给 status 时自动推导
  （failed>0 且 updated>0 -> partial；failed>0 -> failed；否则 success）；
- 抛异常时写入 failed 与 error 后重新抛出，由调用方决定回滚/告警。
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import date
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.sync_run import SyncRun
from app.timezone import now_cn, to_cn

logger = logging.getLogger(__name__)

VALID_STATUSES = ("running", "success", "partial", "failed", "paused")
_FINAL_STATUSES = ("success", "partial", "failed", "paused")


def derive_final_status(updated: int, failed: int, *, processed: int | None = None) -> str:
    """由计数推导任务终态（与 stock_data._final_status 同一约定）。"""
    if updated > 0 and failed > 0:
        return "partial"
    if failed > 0:
        return "failed"
    if (processed if processed is not None else updated + failed) == 0:
        return "partial"
    return "success"


@contextmanager
def track_sync_run(db: Session, job_name: str) -> Iterator[Callable[..., None]]:
    """记录一次同步任务执行的上下文管理器。

    进入时写入 status=running 的记录并提交（保证异常时也留有痕迹）；
    退出时更新为 success/partial/failed/paused。body 内可调用返回的
    record() 补充统计字段或显式覆盖 status。
    """
    run = SyncRun(job_name=job_name, status="running", started_at=now_cn())
    db.add(run)
    db.commit()
    db.refresh(run)

    def record(
        *,
        total: int | None = None,
        updated: int | None = None,
        failed: int | None = None,
        data_date: date | None = None,
        error: str | None = None,
        status: str | None = None,
    ) -> None:
        if total is not None:
            run.total = total
        if updated is not None:
            run.updated = updated
        if failed is not None:
            run.failed = failed
        if data_date is not None:
            run.data_date = data_date
        if error is not None:
            run.error = error
        if status is not None:
            if status not in _FINAL_STATUSES:
                raise ValueError(f"非法终态：{status}（可选 {_FINAL_STATUSES}）")
            run.status = status

    try:
        yield record
    except Exception as exc:
        run.status = "failed"
        run.finished_at = now_cn()
        run.error = f"{type(exc).__name__}: {exc}"
        db.commit()
        raise
    else:
        if run.status not in _FINAL_STATUSES:
            run.status = derive_final_status(run.updated, run.failed, processed=run.total)
        run.finished_at = now_cn()
        db.commit()


def _stored_beijing(dt):
    """SQLite 会丢弃 tzinfo；本表写入的是北京时间墙上时间，不能按 UTC 再加 8 小时。"""
    if dt is None:
        return None
    if dt.tzinfo is None:
        from app.timezone import CN_TZ

        return dt.replace(tzinfo=CN_TZ)
    return dt.astimezone(to_cn(dt).tzinfo)


def _run_to_dict(run: SyncRun) -> dict[str, Any]:
    started_at = _stored_beijing(run.started_at)
    finished_at = _stored_beijing(run.finished_at)
    duration_seconds = None
    if started_at and finished_at:
        duration_seconds = round((finished_at - started_at).total_seconds(), 3)
    return {
        "id": run.id,
        "job_name": run.job_name,
        "status": run.status,
        "started_at": started_at.isoformat() if started_at else None,
        "finished_at": finished_at.isoformat() if finished_at else None,
        "duration_seconds": duration_seconds,
        "total": run.total,
        "updated": run.updated,
        "failed": run.failed,
        "data_date": run.data_date.isoformat() if run.data_date else None,
        "error": run.error,
    }


def get_sync_status(db: Session, job_name: str | None = None) -> dict[str, Any]:
    """查询同步状态。

    - job_name 为空：返回每个任务的最近一次运行记录（按任务名分组）；
    - 指定 job_name：返回该任务最近 20 条运行记录。
    """
    if job_name:
        runs = db.scalars(
            select(SyncRun)
            .where(SyncRun.job_name == job_name)
            .order_by(SyncRun.started_at.desc())
            .limit(20)
        ).all()
        return {
            "server_time": now_cn().isoformat(),
            "job_name": job_name,
            "runs": [_run_to_dict(run) for run in runs],
        }

    latest_ids = (
        select(func.max(SyncRun.id)).group_by(SyncRun.job_name).scalar_subquery()
    )
    runs = db.scalars(
        select(SyncRun).where(SyncRun.id.in_(latest_ids)).order_by(SyncRun.job_name)
    ).all()
    return {
        "server_time": now_cn().isoformat(),
        "job_name": None,
        "runs": [_run_to_dict(run) for run in runs],
    }
