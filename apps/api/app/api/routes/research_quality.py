"""研究数据仓库质量出口：/api/research/quality。

对 DuckDB 研究仓库的 5 个数据集执行声明式质量检查（app.research.quality），
返回各数据集 row_count / ok / issues；空数据集以 warning 显式暴露。
仓库文件不存在时所有数据集按"空"返回并附 warning，不抛 5xx。
"""

from datetime import datetime

from fastapi import APIRouter, Query

from app.config import get_settings
from app.schemas.research import (
    DatasetQualityOut,
    QualityIssueOut,
    ResearchQualityResponse,
)

router = APIRouter(prefix="/research", tags=["research"])


@router.get("/quality", response_model=ResearchQualityResponse)
def get_research_quality(
    gap_max_days: int = Query(default=10, ge=1, le=365, description="日期缺口告警阈值（天）"),
) -> ResearchQualityResponse:
    """返回研究仓库全部 5 个数据集的实时质量报告。"""
    settings = get_settings()
    issues_by_dataset: dict[str, DatasetQualityOut] = {}

    from pathlib import Path

    from app.research.quality import check_all
    from app.research.warehouse import ALL_DATASETS, ResearchWarehouse

    db_path = Path(settings.research_db)
    if not db_path.exists():
        for dataset in ALL_DATASETS:
            issues_by_dataset[dataset] = DatasetQualityOut(
                dataset=dataset,
                row_count=0,
                ok=True,
                issues=[
                    QualityIssueOut(
                        check="warehouse_missing",
                        dataset=dataset,
                        detail=f"研究仓库不存在: {db_path}",
                        severity="warning",
                    )
                ],
            )
        return ResearchQualityResponse(
            generated_at=datetime.now(),
            ok=True,
            datasets=list(issues_by_dataset.values()),
        )

    warehouse = ResearchWarehouse(db_path, settings.research_data_dir, read_only=True)
    try:
        reports = check_all(warehouse, gap_max_days=gap_max_days)
    finally:
        warehouse.close()

    datasets = [
        DatasetQualityOut(
            dataset=name,
            row_count=report.row_count,
            ok=report.ok,
            issues=[
                QualityIssueOut(
                    check=issue.check,
                    dataset=issue.dataset,
                    detail=issue.detail,
                    severity=issue.severity,
                )
                for issue in report.issues
            ],
        )
        for name, report in reports.items()
    ]
    return ResearchQualityResponse(
        generated_at=datetime.now(),
        ok=all(item.ok for item in datasets),
        datasets=datasets,
    )
