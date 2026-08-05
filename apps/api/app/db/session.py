"""数据库 Engine 与 Session 管理。

默认 SQLite 开发环境自动处理 check_same_thread；
PostgreSQL 仅需替换 DATABASE_URL，无需改动代码。
"""

from collections.abc import Iterator

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.config import get_settings


def create_db_engine(database_url: str | None = None) -> Engine:
    """根据连接串创建 Engine，自动处理 SQLite 的连接参数。"""
    url = database_url or get_settings().database_url
    connect_args: dict[str, object] = {}
    if url.startswith("sqlite"):
        # SQLite 默认不允许跨线程使用连接，FastAPI 多线程下需要关闭该检查
        connect_args["check_same_thread"] = False
        # 后台同步任务会短暂写库；等待当前写事务释放，避免立刻报 database is locked。
        connect_args["timeout"] = 30
    settings = get_settings()
    options: dict[str, object] = {"pool_pre_ping": True}
    if url.startswith(("postgresql", "postgres")):
        options.update(
            {
                "pool_size": settings.database_pool_size,
                "max_overflow": settings.database_max_overflow,
                "pool_timeout": settings.database_pool_timeout_seconds,
                "isolation_level": "READ COMMITTED",
                "connect_args": {
                    "options": (
                        f"-c statement_timeout={settings.database_statement_timeout_ms} "
                        f"-c lock_timeout={settings.database_lock_timeout_ms}"
                    )
                },
            }
        )
    else:
        options["connect_args"] = connect_args
    return create_engine(url, **options)


engine = create_db_engine()

SessionLocal = sessionmaker(
    bind=engine,
    autoflush=False,
    autocommit=False,
    expire_on_commit=False,
)


def get_db() -> Iterator[Session]:
    """FastAPI 依赖：每请求一个 Session，结束后自动关闭。"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
