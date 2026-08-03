"""支付宝 PDF 两阶段导入接口。"""

from fastapi import APIRouter, Depends, File, UploadFile
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.models import Import
from app.schemas.imports import (
    CommitResultResponse,
    ImportItem,
    ImportListResponse,
    ImportPreviewResponse,
)
from app.services.importer import commit_import, create_preview

router = APIRouter(prefix="/imports", tags=["imports"])


@router.post("/preview", response_model=ImportPreviewResponse)
def preview_import(
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
) -> ImportPreviewResponse:
    """上传 PDF 并生成解析预览，不写入正式数据。"""
    return create_preview(db, file)


@router.post("/{import_id}/commit", response_model=CommitResultResponse)
def commit_import_route(
    import_id: int,
    db: Session = Depends(get_db),
) -> CommitResultResponse:
    """确认导入解析结果，并保证重复执行不会产生重复数据。"""
    positions_written, transactions_written = commit_import(db, import_id)
    return CommitResultResponse(
        ok=True,
        message=f"已导入持仓 {positions_written} 条、交易 {transactions_written} 条",
        positions_written=positions_written,
        transactions_written=transactions_written,
    )


@router.get("", response_model=ImportListResponse)
def list_imports(db: Session = Depends(get_db)) -> ImportListResponse:
    total = db.scalar(select(func.count(Import.id))) or 0
    records = db.scalars(select(Import).order_by(Import.id.desc()).limit(100)).all()
    return ImportListResponse(
        items=[
            ImportItem(
                id=item.id,
                file_name=item.filename,
                document_type=item.document_type,
                status=item.status.value,
                record_count=item.record_count,
                message=item.message,
                created_at=item.created_at.isoformat(),
            )
            for item in records
        ],
        total=total,
    )
