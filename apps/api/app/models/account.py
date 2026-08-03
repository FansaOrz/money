"""账户模型：资金账户或基金账户，例如某券商/银行/基金公司下的账户。"""

from datetime import datetime

from sqlalchemy import DateTime, String, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


class Account(Base):
    __tablename__ = "accounts"

    id: Mapped[int] = mapped_column(primary_key=True)
    # 账户显示名称，例如“招商银行活期”“天天基金账户”
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    # 机构名称（银行/券商/基金公司）
    institution: Mapped[str | None] = mapped_column(String(100), nullable=True)
    # 对原始账户号做 SHA-256，仅用于关联记录，不保存完整账号
    external_key: Mapped[str | None] = mapped_column(
        String(64), unique=True, nullable=True, index=True
    )
    # 币种，ISO 4217，默认人民币
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="CNY")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    transactions: Mapped[list["Transaction"]] = relationship(  # noqa: F821
        back_populates="account"
    )
    positions: Mapped[list["Position"]] = relationship(  # noqa: F821
        back_populates="account"
    )
