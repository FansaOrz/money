"""基金详情聚合接口测试。"""

from datetime import date
from decimal import Decimal

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models import FundCatalogEntry, FundHolding, FundIndustryAllocation, FundNav, Instrument
from app.services import fund_profile


def _seed(db: Session, code: str = "110022") -> Instrument:
    db.add(
        FundCatalogEntry(
            code=code,
            name="易方达消费行业股票",
            fund_type="股票型",
            market="cn",
            family="易方达消费行业股票",
            active=True,
        )
    )
    instrument = Instrument(code=code, name="易方达消费行业股票")
    db.add(instrument)
    db.flush()
    for index in range(300):
        db.add(
            FundNav(
                instrument_id=instrument.id,
                nav_date=date(2025, 1, 1).replace(day=1) if index == 0 else date.fromordinal(date(2025, 1, 1).toordinal() + index),
                unit_nav=Decimal("1") + Decimal(index) / Decimal("1000"),
                source="test",
            )
        )
    db.commit()
    return instrument


def test_fund_detail_success(
    client: TestClient, db_session: Session, monkeypatch
) -> None:
    instrument = _seed(db_session)
    db_session.add(
        FundHolding(
            instrument_id=instrument.id,
            report_date=date(2026, 6, 30),
            rank=1,
            stock_code="600519",
            stock_name="贵州茅台",
            weight=Decimal("8.5"),
        )
    )
    db_session.add(
        FundIndustryAllocation(
            instrument_id=instrument.id,
            report_date=date(2026, 6, 30),
            industry="食品饮料",
            weight=Decimal("25"),
        )
    )
    db_session.commit()
    monkeypatch.setattr(
        fund_profile,
        "_fetch_xq",
        lambda code: {
            "short_name": "易方达消费行业",
            "full_name": "易方达消费行业股票型证券投资基金",
            "inception_date": date(2010, 8, 20),
            "company": "易方达基金",
            "manager": "萧楠",
            "investment_objective": "追求长期回报",
            "source": "test",
        },
    )

    response = client.get("/api/funds/110022/detail")
    assert response.status_code == 200
    body = response.json()
    assert body["code"] == "110022"
    assert body["profile"]["manager"] == "萧楠"
    assert body["metrics"]["sample_count"] == 300
    assert body["holdings"][0]["stock_code"] == "600519"
    assert body["industries"][0]["industry"] == "食品饮料"


def test_fund_detail_external_profile_failure_degrades(
    client: TestClient, db_session: Session, monkeypatch
) -> None:
    _seed(db_session, "000001")
    monkeypatch.setattr(fund_profile, "_fetch_xq", lambda code: (_ for _ in ()).throw(RuntimeError("xq down")))
    monkeypatch.setattr(fund_profile, "_fetch_eastmoney", lambda code: (_ for _ in ()).throw(RuntimeError("em down")))
    monkeypatch.setattr(fund_profile, "_sync_composition", lambda *args: ["暂无披露"])

    response = client.get("/api/funds/000001/detail")
    assert response.status_code == 200
    assert response.json()["name"]
    assert any("基金介绍暂不可用" in item for item in response.json()["warnings"])


def test_fund_detail_unknown_returns_404(client: TestClient) -> None:
    assert client.get("/api/funds/NOPE/detail").status_code == 404
