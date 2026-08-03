"""pytest 公共夹具。

每个测试使用独立的临时文件 SQLite 数据库，
并通过依赖覆盖替换应用内的 get_db，互不污染。
"""

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from app.db.base import Base
from app.db.session import create_db_engine, get_db
from app.main import create_app


@pytest.fixture()
def db_session(tmp_path: Path) -> Iterator[Session]:
    """独立的临时数据库 Session，测试结束后清理。"""
    db_file = tmp_path / "test.db"
    engine = create_db_engine(f"sqlite:///{db_file}")
    Base.metadata.create_all(bind=engine)
    testing_session = sessionmaker(
        bind=engine, autoflush=False, autocommit=False, expire_on_commit=False
    )
    session = testing_session()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


@pytest.fixture()
def client(db_session: Session) -> Iterator[TestClient]:
    """绑定了临时数据库的 TestClient。"""
    app = create_app()

    def override_get_db() -> Iterator[Session]:
        yield db_session

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()
