"""健康检查路由。"""

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.services import monitoring

router = APIRouter(tags=["health"])


@router.get("/health")
def health_check() -> dict[str, str]:
    """存活探针：服务可用即返回 ok。"""
    return {"status": "ok"}


@router.get("/health/deep")
def deep_health(db: Session = Depends(get_db)) -> dict[str, object]:
    return monitoring.deep_health(db)


@router.get("/metrics")
def structured_metrics(db: Session = Depends(get_db)) -> dict[str, int]:
    return monitoring.metrics(db)
