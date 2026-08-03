"""全市场基金目录模型：东财 fund_name_em 全量基金基本信息的本地镜像。

与 ``instruments`` 表的区别：
- instruments 只登记用户持仓/交易涉及的标的；
- fund_catalog 覆盖全市场公募基金（数万只），用于候选池筛选与净值回填调度。

字段命名尽量贴近 akshare ``fund_name_em`` 返回列（拼音），并额外提供
family / share_class / market / active 等派生字段，全部为幂等 upsert。
"""

from datetime import datetime

from sqlalchemy import Boolean, DateTime, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class FundCatalogEntry(Base):
    """全市场基金目录条目（一只基金一行）。"""

    __tablename__ = "fund_catalog"

    id: Mapped[int] = mapped_column(primary_key=True)
    # 基金代码，例如 110022，全局唯一
    code: Mapped[str] = mapped_column(String(32), unique=True, nullable=False, index=True)
    # 基金全称（拼音简称列保留原值，便于与数据源对账）
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    pinyin_abbr: Mapped[str | None] = mapped_column(String(64), nullable=True)
    pinyin_full: Mapped[str | None] = mapped_column(String(200), nullable=True)
    # 东财一级分类，例如 股票型 / 混合型 / 债券型 / 指数型 / QDII / 货币型
    fund_type: Mapped[str | None] = mapped_column(String(50), nullable=True, index=True)
    # 内部市场分类（复用 quant_factors.classify_market）：cn / cn_300 / hk / us_spx ...
    market: Mapped[str | None] = mapped_column(String(20), nullable=True, index=True)
    # 基金家族（同一基金不同份额共用），例如 易方达消费行业股票
    family: Mapped[str | None] = mapped_column(String(200), nullable=True, index=True)
    # 份额类别：A / C / B / D / E / H / I / O / Y 等；主基金无后缀时为 None
    share_class: Mapped[str | None] = mapped_column(String(10), nullable=True)
    # 是否仍在正常运作（fund_open_fund_daily_em 最新净值列表中出现的为活跃）
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
