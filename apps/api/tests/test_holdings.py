"""组合穿透持仓接口测试。

覆盖：
- 默认 stock_limit=50 / industry_limit=50 截断；
- 完整排序后截断（返回条数 = min(limit, 总数)，排序按穿透占比降序）；
- 响应携带截断前的 stocks_total / industries_total；
- 自定义 limit 参数与越界参数的 422 校验。
"""

from datetime import date
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models import (
    Account,
    FundHolding,
    FundIndustryAllocation,
    Instrument,
    InstrumentType,
    Position,
)

REPORT_DATE = date(2026, 3, 31)


def _seed_portfolio(
    db_session: Session,
    *,
    stock_count: int = 60,
    industry_count: int = 30,
) -> None:
    """写入一只基金及其持仓/行业配置，使穿透结果有确定数量的条目。"""
    account = Account(name="测试账户", institution="测试机构")
    instrument = Instrument(
        code="110022", name="测试基金", type=InstrumentType.FUND, currency="CNY"
    )
    db_session.add_all([account, instrument])
    db_session.flush()

    position = Position(
        account_id=account.id,
        instrument_id=instrument.id,
        shares=Decimal("1000.0000"),
        cost=Decimal("1000.00"),
        market_value=Decimal("1000.00"),
    )
    db_session.add(position)

    # 股票权重递减，保证排序结果确定（总和无需等于 100，接口不校验）
    for i in range(stock_count):
        db_session.add(
            FundHolding(
                instrument_id=instrument.id,
                report_date=REPORT_DATE,
                rank=i + 1,
                stock_code=f"S{i:04d}",
                stock_name=f"股票{i:04d}",
                weight=Decimal(str(stock_count - i)) / Decimal("10"),
                shares=None,
                market_value=None,
            )
        )
    for i in range(industry_count):
        db_session.add(
            FundIndustryAllocation(
                instrument_id=instrument.id,
                report_date=REPORT_DATE,
                industry=f"行业{i:03d}",
                weight=Decimal(str(industry_count - i)) / Decimal("10"),
                market_value=None,
            )
        )
    db_session.commit()


def test_exposure_empty(client: TestClient) -> None:
    """空库时列表为空，total 为 0。"""
    response = client.get("/api/holdings/portfolio/exposure")
    assert response.status_code == 200
    data = response.json()
    assert data["stocks"] == []
    assert data["industries"] == []
    assert data["stocks_total"] == 0
    assert data["industries_total"] == 0
    assert data["total_market_value"] == "0"


def test_exposure_default_limits(
    client: TestClient, db_session: Session
) -> None:
    """默认各返回 50 条，total 字段反映截断前总数。"""
    _seed_portfolio(db_session, stock_count=60, industry_count=30)
    response = client.get("/api/holdings/portfolio/exposure")
    assert response.status_code == 200
    data = response.json()
    assert len(data["stocks"]) == 50
    assert data["stocks_total"] == 60
    # 行业总数 30 < 默认 50，全量返回
    assert len(data["industries"]) == 30
    assert data["industries_total"] == 30


def test_exposure_sorted_before_truncation(
    client: TestClient, db_session: Session
) -> None:
    """先按穿透占比降序完整排序，再截断：第一条应为权重最高的股票。"""
    _seed_portfolio(db_session, stock_count=60, industry_count=30)
    data = client.get("/api/holdings/portfolio/exposure").json()
    weights = [Decimal(s["portfolio_weight"]) for s in data["stocks"]]
    assert weights == sorted(weights, reverse=True)
    # 种子数据中 S0000 权重最高
    assert data["stocks"][0]["code"] == "S0000"
    assert data["industries"][0]["code"] == "行业000"


def test_exposure_custom_limits(
    client: TestClient, db_session: Session
) -> None:
    """自定义 limit 生效，total 不受影响。"""
    _seed_portfolio(db_session, stock_count=60, industry_count=30)
    response = client.get(
        "/api/holdings/portfolio/exposure",
        params={"stock_limit": 100, "industry_limit": 10},
    )
    assert response.status_code == 200
    data = response.json()
    assert len(data["stocks"]) == 60
    assert data["stocks_total"] == 60
    assert len(data["industries"]) == 10
    assert data["industries_total"] == 30


def test_exposure_max_limits_return_all(
    client: TestClient, db_session: Session
) -> None:
    """取上限值时返回全部条目（前端一次请求全量的用法）。"""
    _seed_portfolio(db_session, stock_count=60, industry_count=30)
    response = client.get(
        "/api/holdings/portfolio/exposure",
        params={"stock_limit": 2000, "industry_limit": 500},
    )
    assert response.status_code == 200
    data = response.json()
    assert len(data["stocks"]) == data["stocks_total"] == 60
    assert len(data["industries"]) == data["industries_total"] == 30


@pytest.mark.parametrize(
    "params",
    [
        {"stock_limit": 0},
        {"stock_limit": 2001},
        {"stock_limit": -1},
        {"industry_limit": 0},
        {"industry_limit": 501},
        {"stock_limit": "abc"},
    ],
)
def test_exposure_limit_validation(
    client: TestClient, db_session: Session, params: dict
) -> None:
    """limit 越界或非法时返回 422。"""
    _seed_portfolio(db_session, stock_count=5, industry_count=5)
    response = client.get("/api/holdings/portfolio/exposure", params=params)
    assert response.status_code == 422


@pytest.mark.parametrize(
    "params",
    [
        {"stock_limit": 1, "industry_limit": 1},
        {"stock_limit": 2000, "industry_limit": 500},
    ],
)
def test_exposure_limit_boundary_ok(
    client: TestClient, db_session: Session, params: dict
) -> None:
    """边界值 1 / 2000 / 500 合法。"""
    _seed_portfolio(db_session, stock_count=5, industry_count=5)
    response = client.get("/api/holdings/portfolio/exposure", params=params)
    assert response.status_code == 200
