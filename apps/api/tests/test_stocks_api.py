"""A 股研究数据层路由冒烟测试（外部数据全部 mock，离线）。"""

from __future__ import annotations

import io

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from app.services.research import ak_fetch


@pytest.fixture(autouse=True)
def mock_ak(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        ak_fetch,
        "fetch_stock_code_name",
        lambda: pd.DataFrame(
            [{"code": "600519", "name": "贵州茅台"}, {"code": "000001", "name": "平安银行"}]
        ),
    )
    monkeypatch.setattr(
        ak_fetch,
        "fetch_stock_daily_sina",
        lambda symbol, adjust="": pd.DataFrame(
            [
                {
                    "date": "2024-01-02", "open": 10.0, "high": 10.5, "low": 9.8,
                    "close": 10.2, "volume": 1000, "amount": 10200.0,
                    "outstanding_share": 1e8, "turnover": 0.5,
                }
            ]
        ),
    )
    monkeypatch.setattr(
        ak_fetch,
        "fetch_index_cons",
        lambda symbol: pd.DataFrame(
            [{"成分券代码": "600519", "成分券名称": "贵州茅台", "纳入日期": "2020-06-15"}]
        ),
    )


@pytest.fixture(autouse=True)
def research_root(tmp_path, monkeypatch: pytest.MonkeyPatch):
    from app.services.research import parquet_store

    root = tmp_path / "research"
    original = parquet_store.data_root
    monkeypatch.setattr(parquet_store, "data_root", lambda r=None: original(root))
    return root


def test_full_flow(client: TestClient) -> None:
    # master 同步
    resp = client.post("/api/stocks/sync/master")
    assert resp.status_code == 200
    assert resp.json()["total"] == 2

    # 日线同步（指定单只）
    resp = client.post("/api/stocks/sync/daily", params={"codes": "600519", "with_qfq": True})
    assert resp.status_code == 200
    body = resp.json()
    assert body["updated"] == 1 and body["failed"] == 0

    # 读取日线
    resp = client.get("/api/stocks/600519/daily")
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 1
    assert body["items"][0]["trade_date"] == "2024-01-02"

    # universe 同步 + 查询
    resp = client.post("/api/stocks/sync/universe", params={"index_codes": "000300"})
    assert resp.status_code == 200
    resp = client.get("/api/stocks/universe", params={"index_code": "000300"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["basis"] == "current"
    assert body["total"] == 1

    # 事件 CSV 导入 + 历史快照
    csv_text = (
        "index_code,stock_code,stock_name,event_type,effective_date\n"
        "000300,600519,贵州茅台,add,2020-06-15\n"
    )
    resp = client.post(
        "/api/stocks/universe/events",
        files={"file": ("events.csv", io.BytesIO(csv_text.encode("utf-8")), "text/csv")},
    )
    assert resp.status_code == 200
    assert resp.json()["imported"] == 1

    resp = client.post(
        "/api/stocks/universe/snapshot",
        params={"index_code": "000300", "as_of": "2020-12-31"},
    )
    assert resp.status_code == 200
    assert resp.json()["members"] == 1

    resp = client.get(
        "/api/stocks/universe", params={"index_code": "000300", "as_of": "2020-12-31"}
    )
    assert resp.json()["basis"] == "snapshot"

    # status 汇总
    resp = client.get("/api/stocks/data/status")
    assert resp.status_code == 200
    body = resp.json()
    assert body["master"]["stocks"] == 2
    assert body["daily"]["stocks_with_parquet"] == 1
    assert body["universe"]["constituents"] == {"000300": 1}
    assert body["universe"]["membership_events"] == 1


def test_daily_layer_validation(client: TestClient) -> None:
    resp = client.get("/api/stocks/600519/daily", params={"layer": "hfq"})
    assert resp.status_code == 400


def test_network_failure_graceful(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ak_fetch, "fetch_stock_code_name", lambda: None)
    resp = client.post("/api/stocks/sync/master")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "failed"
    assert body["errors"]
