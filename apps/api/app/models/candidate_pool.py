"""候选池模型：一次建池结果（Pool）与其成员（Member）。

候选池是“从全市场目录中筛出、值得做净值回填与量化研究”的基金集合：
- 每次 build 生成一个新 Pool（默认保留历史，便于回溯）；
- Member 记录入池时的过滤状态、分层、家族去重结果与净值就绪状态；
- build 只负责建池，不触发全历史净值回填（回填由后续任务按池内代码调度）。
"""

from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


class CandidatePool(Base):
    """一次候选池构建（一次 build 一行）。"""

    __tablename__ = "candidate_pools"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    # 构建参数快照（JSON 字符串），便于复现与审计
    params: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 池目标规模上限（默认 800，允许 500~1000）
    max_size: Mapped[int] = mapped_column(Integer, nullable=False, default=800)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="ready")
    member_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    members: Mapped[list["CandidatePoolMember"]] = relationship(
        back_populates="pool",
        cascade="all, delete-orphan",
        order_by="CandidatePoolMember.rank",
    )


class CandidatePoolMember(Base):
    """候选池成员：一只入选基金及其建池时的元信息。

    status 取值：
    - active：正常入选，等待/已回填净值；
    - excluded：入选后因数据缺失等原因被剔除（保留行便于审计）。

    nav_ready：库内净值样本是否达到研究所需下限（见 candidate_pool 服务）。
    """

    __tablename__ = "candidate_pool_members"
    __table_args__ = (
        UniqueConstraint("pool_id", "code", name="uq_pool_member_code"),
        Index("ix_pool_members_pool_status", "pool_id", "status"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    pool_id: Mapped[int] = mapped_column(
        ForeignKey("candidate_pools.id", ondelete="CASCADE"), nullable=False, index=True
    )
    code: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    fund_type: Mapped[str | None] = mapped_column(String(50), nullable=True)
    market: Mapped[str | None] = mapped_column(String(20), nullable=True)
    family: Mapped[str | None] = mapped_column(String(200), nullable=True)
    share_class: Mapped[str | None] = mapped_column(String(10), nullable=True)
    # 分层（tier）：1 核心权益 / 2 次级权益 / 3 观察（黄金/债券/货币/海外）
    tier: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    # 池内排序（按分层配额与打分规则确定，小在前）
    rank: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="active")
    # 建池时库内净值样本数（尚未回填时为 0）
    nav_samples: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    nav_ready: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    pool: Mapped[CandidatePool] = relationship(back_populates="members")
