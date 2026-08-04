"""同步状态查询路由：查看各定时任务最近一次/近期运行情况。"""

from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session
from sqlalchemy import select

from app.db.session import get_db
from app.models import DataQualityIssue, PersistentJob
from app.services import sync_status as sync_status_service

router = APIRouter(prefix="/sync", tags=["sync"])


@router.get("/status")
def get_sync_status(
    job_name: str | None = Query(default=None, description="任务名，如 fund_nav/indices/news/holdings/paper"),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """查询同步任务运行状态。

    - 不带参数：返回每个任务最近一次运行的状态汇总；
    - 带 job_name：返回该任务最近 20 次运行记录。

    所有时间字段均为北京时间（ISO 8601，带 +08:00 时区偏移）。
    """
    result = sync_status_service.get_sync_status(db, job_name=job_name)
    if job_name is None:
        # 延迟导入，避免 main -> route -> scheduler -> main 的循环依赖。
        from app.services.scheduler import next_run_times

        result["next_runs"] = {
            name: run_at.isoformat() for name, run_at in next_run_times().items()
        }
        failed_jobs = db.scalars(
            select(PersistentJob)
            .where(PersistentJob.status == "failed")
            .order_by(PersistentJob.finished_at.desc())
            .limit(20)
        ).all()
        quality_issues = db.scalars(
            select(DataQualityIssue)
            .where(DataQualityIssue.status == "open")
            .order_by(DataQualityIssue.detected_at.desc())
            .limit(20)
        ).all()
        result["alerts"] = [
            {
                "type": "job_failed",
                "severity": "error",
                "message": f"{row.job_name}: {row.error or '重试耗尽'}",
                "correlation_id": row.correlation_id,
            }
            for row in failed_jobs
        ] + [
            {
                "type": "data_quality",
                "severity": row.severity,
                "message": row.detail,
                "code": row.code,
            }
            for row in quality_issues
        ]
    return result
