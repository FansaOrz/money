"""基金详情与按需刷新接口。"""

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.schemas.funds import FundDetailResponse, FundRefreshResponse
from app.services import fund_profile as fund_profile_service

router = APIRouter(prefix="/funds", tags=["funds"])


@router.get("/{code}/detail", response_model=FundDetailResponse)
def fund_detail(code: str, db: Session = Depends(get_db)) -> FundDetailResponse:
    """基金介绍、量化指标及最新季度披露；子数据源失败时局部降级。"""
    detail = fund_profile_service.build_detail(db, code, refresh=False)
    if detail is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"基金不存在：{code}")
    return detail


@router.post("/{code}/refresh", response_model=FundRefreshResponse)
def refresh_fund_detail(code: str, db: Session = Depends(get_db)) -> FundRefreshResponse:
    """按需刷新基金介绍与季度披露，不产生任何交易。"""
    detail = fund_profile_service.build_detail(db, code, refresh=True)
    if detail is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"基金不存在：{code}")
    return FundRefreshResponse(**detail.model_dump(), refreshed=True)
