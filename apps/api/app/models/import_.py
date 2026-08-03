"""导入批次模型：记录每次对账单（PDF 等）导入的元信息。

解析器暂未实现，此表为后续导入功能预留的落库结构。
"""

import enum
from datetime import datetime

from sqlalchemy import DateTime, Enum, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


class ImportStatus(str, enum.Enum):
    """导入批次状态机。"""

    PENDING = "pending"        # 已上传，待解析
    PROCESSING = "processing"  # 解析中
    COMPLETED = "completed"    # 解析完成
    FAILED = "failed"          # 解析失败
    DUPLICATE = "duplicate"    # 文件已导入


class Import(Base):
    __tablename__ = "imports"

    id: Mapped[int] = mapped_column(primary_key=True)
    # 原始文件名（对账单 PDF）
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    # 文件内容 SHA-256，用于去重
    file_hash: Mapped[str | None] = mapped_column(String(64), unique=True, nullable=True)
    status: Mapped[ImportStatus] = mapped_column(
        Enum(ImportStatus, native_enum=False, length=20),
        nullable=False,
        default=ImportStatus.PENDING,
        server_default=ImportStatus.PENDING.value,
    )
    document_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    record_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # 解析失败原因或导入摘要；预览明细只保留在内存，不写入数据库
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    committed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    transactions: Mapped[list["Transaction"]] = relationship(  # noqa: F821
        back_populates="import_batch"
    )
