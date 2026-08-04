"""FastAPI 应用入口。

包含：CORS、路由注册、启动时自动建表。
"""

# 注意：contextlib 必须先于 collections.abc 导入。
# Python 3.13 中若 collections.abc 先行，会污染 contextlib 对
# AsyncGenerator 的类型判定，导致 @asynccontextmanager 退化为同步生成器。
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.routes import (
    discovery,
    discovery_quant,
    health,
    holdings,
    funds,
    imports,
    indices,
    news,
    paper,
    portfolio,
    positions,
    quant,
    quant_v2,
    quant_governance,
    research_portfolios,
    research_quality,
    stocks,
    stock_paper,
    stocks_research,
    sync_status,
    transactions,
)
from app.config import get_settings
from app.db.base import Base
from app.db.session import engine
from app import models  # noqa: F401  # 确保所有模型已注册到 Base.metadata
from app.services.security import ApiSecurityMiddleware


def create_tables() -> None:
    """仅开发/测试的便利建表；生产结构必须由 Alembic 管理。"""
    if get_settings().environment.lower() == "production":
        raise RuntimeError(
            "production 禁止 create_all；请先执行 `alembic upgrade head`"
        )
    Base.metadata.create_all(bind=engine)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """应用生命周期：启动时按需自动建表。"""
    settings = get_settings()
    if settings.auto_create_tables:
        create_tables()
    yield


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(title=settings.app_name, lifespan=lifespan)
    app.add_middleware(ApiSecurityMiddleware)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(health.router, prefix="/api")
    app.include_router(portfolio.router, prefix="/api")
    app.include_router(imports.router, prefix="/api")
    app.include_router(positions.router, prefix="/api")
    app.include_router(transactions.router, prefix="/api")
    app.include_router(news.router, prefix="/api")
    app.include_router(quant.router, prefix="/api")
    app.include_router(quant_v2.router, prefix="/api")
    app.include_router(quant_governance.router, prefix="/api")
    app.include_router(holdings.router, prefix="/api")
    app.include_router(funds.router, prefix="/api")
    app.include_router(indices.router, prefix="/api")
    app.include_router(paper.router, prefix="/api")
    app.include_router(sync_status.router, prefix="/api")
    app.include_router(discovery.router, prefix="/api")
    app.include_router(discovery_quant.router, prefix="/api")
    app.include_router(stocks.router, prefix="/api")
    app.include_router(stock_paper.router, prefix="/api")
    app.include_router(stocks_research.router, prefix="/api")
    app.include_router(research_portfolios.router, prefix="/api")
    app.include_router(research_quality.router, prefix="/api")
    return app


app = create_app()
