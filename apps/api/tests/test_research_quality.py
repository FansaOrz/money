"""GET /api/research/quality 路由测试。

覆盖：
- 仓库不存在时 5 个数据集均返回 row_count=0 + warning，不抛 5xx；
- 空仓库时每个数据集带 empty warning（空仓明确暴露）；
- 有数据时 row_count 正确、ok 聚合正确；
- error 级质量问题（负价格）使 overall ok=False。
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date
from pathlib import Path

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.main import create_app


@pytest.fixture()
def quality_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """研究仓库指向临时目录的 TestClient。"""
    monkeypatch.setenv("MONEY_RESEARCH_DB", str(tmp_path / "research.duckdb"))
    monkeypatch.setenv("MONEY_RESEARCH_DATA_DIR", str(tmp_path / "lake"))
    monkeypatch.setenv("MONEY_DATABASE_URL", f"sqlite:///{tmp_path / 'biz.db'}")
    get_settings.cache_clear()
    app = create_app()
    with TestClient(app) as client:
        yield client
    get_settings.cache_clear()


def _warehouse(tmp_path: Path):
    from app.research.warehouse import ResearchWarehouse

    wh = ResearchWarehouse(tmp_path / "research.duckdb", tmp_path / "lake")
    wh.init_schemas()
    return wh


def test_quality_warehouse_missing(quality_client: TestClient) -> None:
    resp = quality_client.get("/api/research/quality")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert len(body["datasets"]) == 5
    names = {d["dataset"] for d in body["datasets"]}
    assert names == {
        "fund_nav", "stock_daily", "universe_membership", "fundamentals", "factor_panel",
    }
    for dataset in body["datasets"]:
        assert dataset["row_count"] == 0
        assert dataset["ok"] is True
        assert any(i["severity"] == "warning" for i in dataset["issues"])


def test_quality_empty_warehouse(quality_client: TestClient, tmp_path: Path) -> None:
    wh = _warehouse(tmp_path)
    wh.close()
    resp = quality_client.get("/api/research/quality")
    assert resp.status_code == 200
    body = resp.json()
    for dataset in body["datasets"]:
        assert dataset["row_count"] == 0
        assert any(i["check"] == "empty" and i["severity"] == "warning" for i in dataset["issues"])
    assert body["ok"] is True  # 空只是 warning


def test_quality_with_data(quality_client: TestClient, tmp_path: Path) -> None:
    from app.research.repository import DuckDBRepository

    wh = _warehouse(tmp_path)
    repo = DuckDBRepository(wh, auto_init=False)
    repo.write_fund_nav(
        pd.DataFrame(
            {
                "fund_code": ["110022", "110022"],
                "effective_date": [date(2024, 1, 2), date(2024, 1, 3)],
                "nav": [1.0, 1.01],
            }
        ),
        source="eastmoney",
    )
    wh.close()

    resp = quality_client.get("/api/research/quality")
    assert resp.status_code == 200
    body = resp.json()
    by_name = {d["dataset"]: d for d in body["datasets"]}
    assert by_name["fund_nav"]["row_count"] == 2
    assert by_name["fund_nav"]["ok"] is True
    assert by_name["stock_daily"]["row_count"] == 0
    assert any(i["check"] == "empty" for i in by_name["stock_daily"]["issues"])
    assert body["ok"] is True


def test_quality_error_propagates(quality_client: TestClient, tmp_path: Path) -> None:
    from app.research.repository import DuckDBRepository

    wh = _warehouse(tmp_path)
    repo = DuckDBRepository(wh, auto_init=False)
    repo.write_fund_nav(
        pd.DataFrame(
            {
                "fund_code": ["110022"],
                "effective_date": [date(2024, 1, 2)],
                "nav": [-1.0],
            }
        ),
        source="eastmoney",
    )
    wh.close()

    resp = quality_client.get("/api/research/quality")
    assert resp.status_code == 200
    body = resp.json()
    by_name = {d["dataset"]: d for d in body["datasets"]}
    assert by_name["fund_nav"]["ok"] is False
    assert any(i["check"] == "negative_prices" for i in by_name["fund_nav"]["issues"])
    assert body["ok"] is False
