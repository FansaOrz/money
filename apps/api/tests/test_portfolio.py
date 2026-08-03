"""组合汇总与持仓列表接口测试。"""

from fastapi.testclient import TestClient


def test_summary_empty(client: TestClient) -> None:
    """空库时汇总为零值。"""
    response = client.get("/api/portfolio/summary")
    assert response.status_code == 200
    data = response.json()
    assert data["total_cost"] == "0"
    assert data["total_market_value"] == "0"
    assert data["total_profit"] == "0"
    assert data["profit_rate"] is None
    assert data["position_count"] == 0
    assert data["currency"] == "CNY"


def test_positions_empty(client: TestClient) -> None:
    """空库时持仓列表为空。"""
    response = client.get("/api/portfolio/positions")
    assert response.status_code == 200
    assert response.json() == {"items": [], "total": 0}


def test_seed_position_and_summary(client: TestClient) -> None:
    """录入持仓后，列表与汇总应正确反映 Decimal 金额。"""
    payload = {
        "account_name": "天天基金账户",
        "instrument_code": "110022",
        "instrument_name": "易方达消费行业股票",
        "shares": "1000.0000",
        "cost": "1500.00",
        "market_value": "1650.00",
    }
    created = client.post("/api/portfolio/positions", json=payload)
    assert created.status_code == 201
    item = created.json()
    assert item["instrument_code"] == "110022"
    assert item["shares"] == "1000.0000"
    assert item["cost"] == "1500.00"
    assert item["market_value"] == "1650.00"
    assert item["profit"] == "150.00"
    # 150 / 1500 = 0.1
    assert item["profit_rate"] == "0.1"

    positions = client.get("/api/portfolio/positions").json()
    assert positions["total"] == 1
    assert positions["items"][0]["account_name"] == "全部账户"

    summary = client.get("/api/portfolio/summary").json()
    assert summary["total_cost"] == "1500.00"
    assert summary["total_market_value"] == "1650.00"
    assert summary["total_profit"] == "150.00"
    assert summary["profit_rate"] == "0.1"
    assert summary["position_count"] == 1


def test_seed_position_accumulates(client: TestClient) -> None:
    """同账户同标的重复录入时份额与成本累加。"""
    payload = {
        "account_name": "天天基金账户",
        "instrument_code": "110022",
        "instrument_name": "易方达消费行业股票",
        "shares": "1000.0000",
        "cost": "1500.00",
    }
    assert client.post("/api/portfolio/positions", json=payload).status_code == 201
    assert client.post("/api/portfolio/positions", json=payload).status_code == 201

    positions = client.get("/api/portfolio/positions").json()
    assert positions["total"] == 1
    assert positions["items"][0]["shares"] == "2000.0000"
    assert positions["items"][0]["cost"] == "3000.00"


def test_seed_position_validation(client: TestClient) -> None:
    """份额必须为正数，非法输入返回 422。"""
    payload = {
        "account_name": "天天基金账户",
        "instrument_code": "110022",
        "instrument_name": "易方达消费行业股票",
        "shares": "0",
        "cost": "1500.00",
    }
    response = client.post("/api/portfolio/positions", json=payload)
    assert response.status_code == 422


def test_summary_falls_back_to_cost_without_market_value(client: TestClient) -> None:
    """无市值信息时，汇总市值回退为成本，盈亏为 0。"""
    payload = {
        "account_name": "招商银行",
        "instrument_code": "000001",
        "instrument_name": "华夏成长混合",
        "shares": "500.0000",
        "cost": "800.00",
    }
    assert client.post("/api/portfolio/positions", json=payload).status_code == 201

    summary = client.get("/api/portfolio/summary").json()
    assert summary["total_cost"] == "800.00"
    assert summary["total_market_value"] == "800.00"
    assert summary["total_profit"] == "0.00"
    assert summary["profit_rate"] == "0"
