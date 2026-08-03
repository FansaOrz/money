"""组合区间收益接口测试（GET /api/portfolio/returns）。

使用合成的确定性净值序列与持仓，不依赖外部行情。
"""

from datetime import date, timedelta
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    Account,
    FundNav,
    Instrument,
    Position,
    Transaction,
    TransactionType,
)

END_DATE = date(2026, 7, 30)


def _add_nav(
    db: Session,
    instrument: Instrument,
    nav_date: date,
    unit_nav: str,
    accumulated_nav: str | None = None,
) -> None:
    db.add(
        FundNav(
            instrument_id=instrument.id,
            nav_date=nav_date,
            unit_nav=Decimal(unit_nav),
            accumulated_nav=Decimal(accumulated_nav) if accumulated_nav else None,
            source="test",
        )
    )


def _seed_fund(
    db: Session,
    code: str,
    name: str,
    shares: str,
    navs: list[tuple[date, str, str | None]],
) -> Instrument:
    """写入一只基金、一个账户、一条持仓以及给定净值序列。"""
    instrument = Instrument(code=code, name=name)
    db.add(instrument)
    db.flush()
    account = Account(name=f"账户-{code}")
    db.add(account)
    db.flush()
    db.add(
        Position(
            account_id=account.id,
            instrument_id=instrument.id,
            shares=Decimal(shares),
            cost=Decimal("1000.00"),
        )
    )
    for nav_date, unit_nav, acc_nav in navs:
        _add_nav(db, instrument, nav_date, unit_nav, acc_nav)
    db.commit()
    return instrument


def test_returns_empty(client: TestClient) -> None:
    """无持仓时返回空窗口集合。"""
    response = client.get("/api/portfolio/returns")
    assert response.status_code == 200
    assert response.json() == {"windows": {}}


def test_returns_all_windows(client: TestClient, db_session: Session) -> None:
    """默认一次返回 1d / 1w / 1m / 3m 四个窗口。"""
    navs = [
        (END_DATE - timedelta(days=100), "1.0000", "1.2000"),
        (END_DATE - timedelta(days=30), "1.1000", "1.3200"),
        (END_DATE - timedelta(days=7), "1.1500", "1.3800"),
        (END_DATE - timedelta(days=1), "1.1800", "1.4160"),
        (END_DATE, "1.2000", "1.4400"),
    ]
    _seed_fund(db_session, "110022", "测试基金", "1000.0000", navs)

    response = client.get("/api/portfolio/returns")
    assert response.status_code == 200
    data = response.json()
    assert set(data["windows"].keys()) == {"1d", "1w", "1m", "3m"}

    # 1d：金额 = 1000 * (1.2 - 1.18) = 20；累计净值比 1.44 / 1.416 - 1
    one_day = data["windows"]["1d"]
    assert one_day["coverage"] == "1"
    assert one_day["available_count"] == 1
    assert one_day["stale_count"] == 0
    assert one_day["as_of_end_date"] == END_DATE.isoformat()
    assert float(one_day["return_amount"]) == pytest.approx(20)
    assert float(one_day["return_rate"]) == pytest.approx(1.44 / 1.416 - 1, rel=1e-9)

    item = one_day["items"][0]
    assert item["instrument_code"] == "110022"
    assert item["status"] == "available"
    assert item["rate_basis"] == "accumulated"
    assert item["start_date"] == (END_DATE - timedelta(days=1)).isoformat()
    assert item["end_date"] == END_DATE.isoformat()
    assert item["has_flows"] is False
    assert float(item["weight"]) == pytest.approx(1)

    # 3m 起点目标为 END_DATE 前移 3 个月，应取 <= 该日期的最后一条（100 天前）
    three_month = data["windows"]["3m"]
    assert three_month["items"][0]["start_date"] == (
        END_DATE - timedelta(days=100)
    ).isoformat()


def test_returns_single_window_query(client: TestClient, db_session: Session) -> None:
    """query 指定单窗口时只返回该窗口。"""
    navs = [
        (END_DATE - timedelta(days=7), "1.0000", None),
        (END_DATE, "1.0500", None),
    ]
    _seed_fund(db_session, "110022", "测试基金", "2000.0000", navs)

    response = client.get("/api/portfolio/returns", params={"window": "1w"})
    assert response.status_code == 200
    data = response.json()
    assert set(data["windows"].keys()) == {"1w"}

    window = data["windows"]["1w"]
    item = window["items"][0]
    # 无累计净值时回退单位净值比
    assert item["rate_basis"] == "unit"
    assert float(item["return_rate"]) == pytest.approx(0.05, rel=1e-9)
    # 金额 = 2000 * (1.05 - 1.0) = 100
    assert float(item["return_amount"]) == pytest.approx(100)


def test_returns_invalid_window(client: TestClient, db_session: Session) -> None:
    """非法窗口返回 400。"""
    navs = [(END_DATE, "1.0000", None)]
    _seed_fund(db_session, "110022", "测试基金", "100.0000", navs)
    response = client.get("/api/portfolio/returns", params={"window": "1y"})
    assert response.status_code == 400


def test_returns_start_uses_last_nav_on_or_before_target(
    client: TestClient, db_session: Session
) -> None:
    """起点取 <= 目标日期的最后一条净值（非交易日回退）。"""
    # 目标起点为 END_DATE - 7；7 天前无净值，更早一条在 9 天前
    navs = [
        (END_DATE - timedelta(days=9), "1.0000", None),
        (END_DATE - timedelta(days=6), "1.5000", None),
        (END_DATE, "1.1000", None),
    ]
    _seed_fund(db_session, "110022", "测试基金", "1000.0000", navs)

    data = client.get("/api/portfolio/returns", params={"window": "1w"}).json()
    item = data["windows"]["1w"]["items"][0]
    assert item["start_date"] == (END_DATE - timedelta(days=9)).isoformat()
    assert float(item["return_amount"]) == pytest.approx(100)


def test_returns_stale_without_navs(client: TestClient, db_session: Session) -> None:
    """无净值数据的基金标记为 stale，不纳入组合加权。"""
    _seed_fund(db_session, "110022", "测试基金", "1000.0000", [])

    data = client.get("/api/portfolio/returns", params={"window": "1d"}).json()
    window = data["windows"]["1d"]
    item = window["items"][0]
    assert item["status"] == "stale"
    assert item["stale_reason"] is not None
    assert item["return_amount"] is None
    # 全部 stale：组合收益与 coverage 为空/零
    assert window["stale_count"] == 1
    assert window["return_amount"] is None
    assert window["return_rate"] is None
    assert Decimal(window["coverage"]) == 0


def test_returns_stale_when_start_missing(client: TestClient, db_session: Session) -> None:
    """起点目标日期之前无净值时标记 stale 并说明。"""
    navs = [(END_DATE, "1.0000", None)]
    _seed_fund(db_session, "110022", "测试基金", "1000.0000", navs)

    data = client.get("/api/portfolio/returns", params={"window": "1m"}).json()
    item = data["windows"]["1m"]["items"][0]
    assert item["status"] == "stale"
    assert "起点" in item["stale_reason"]
    assert item["end_date"] == END_DATE.isoformat()


def test_returns_approximate_with_flows(client: TestClient, db_session: Session) -> None:
    """窗口内存在 BUY/SELL/REINVEST 流水时标记 approximate。"""
    instrument = _seed_fund(
        db_session,
        "110022",
        "测试基金",
        "1000.0000",
        [
            (END_DATE - timedelta(days=1), "1.0000", None),
            (END_DATE, "1.1000", None),
        ],
    )
    account = db_session.scalar(
        select(Account).where(Account.name == f"账户-{instrument.code}")
    )
    db_session.add(
        Transaction(
            account_id=account.id,
            instrument_id=instrument.id,
            type=TransactionType.BUY,
            trade_date=END_DATE,
            shares=Decimal("100"),
            nav=Decimal("1.1"),
            amount=Decimal("110.00"),
        )
    )
    db_session.commit()

    data = client.get("/api/portfolio/returns", params={"window": "1d"}).json()
    item = data["windows"]["1d"]["items"][0]
    assert item["status"] == "approximate"
    assert item["has_flows"] is True
    # approximate 仍纳入组合加权
    window = data["windows"]["1d"]
    assert window["approximate_count"] == 1
    assert window["return_amount"] is not None

    # 窗口之前的流水不影响判定
    db_session.add(
        Transaction(
            account_id=account.id,
            instrument_id=instrument.id,
            type=TransactionType.SELL,
            trade_date=END_DATE - timedelta(days=10),
            shares=Decimal("-50"),
            nav=Decimal("1.0"),
            amount=Decimal("50.00"),
        )
    )
    db_session.commit()
    data = client.get("/api/portfolio/returns", params={"window": "1d"}).json()
    assert data["windows"]["1d"]["items"][0]["status"] == "approximate"  # 窗口内仍有一笔
    data = client.get("/api/portfolio/returns", params={"window": "1d"}).json()
    assert data["windows"]["1d"]["items"][0]["has_flows"] is True


def test_returns_portfolio_weighting_and_coverage(
    client: TestClient, db_session: Session
) -> None:
    """组合按期末金额加权，coverage 反映可用金额占比。"""
    # 基金 A：期末金额 1000 * 2 = 2000，收益 1000 * (2 - 1.8) = 200，起点金额 1800
    _seed_fund(
        db_session,
        "110022",
        "基金A",
        "1000.0000",
        [
            (END_DATE - timedelta(days=1), "1.8000", None),
            (END_DATE, "2.0000", None),
        ],
    )
    # 基金 B：期末金额 1000 * 1 = 1000，收益 1000 * (1 - 0.9) = 100
    _seed_fund(
        db_session,
        "000001",
        "基金B",
        "1000.0000",
        [
            (END_DATE - timedelta(days=1), "0.9000", None),
            (END_DATE, "1.0000", None),
        ],
    )
    # 基金 C：无净值，stale，不纳入
    _seed_fund(db_session, "519888", "基金C", "500.0000", [])

    data = client.get("/api/portfolio/returns", params={"window": "1d"}).json()
    window = data["windows"]["1d"]
    # 组合收益 = 200 + 100 = 300；基数 = 1800 + 900 = 2700（起点金额）
    assert float(window["return_amount"]) == pytest.approx(300)
    assert float(window["return_rate"]) == pytest.approx(300 / 2700, rel=1e-9)
    assert float(window["coverage"]) == pytest.approx(1)
    assert window["available_count"] == 2
    assert window["stale_count"] == 1

    weights = {item["instrument_code"]: item["weight"] for item in window["items"]}
    # 权重按期末金额：A = 2000/3000，B = 1000/3000，C 无净值权重为 None
    assert float(weights["110022"]) == pytest.approx(2 / 3, rel=1e-9)
    assert float(weights["000001"]) == pytest.approx(1 / 3, rel=1e-9)
    assert weights["519888"] is None


def test_returns_qdii_uses_own_latest_date(
    client: TestClient, db_session: Session
) -> None:
    """QDII 按自身最新净值计算，披露滞后时给出 stale_reason 说明。"""
    # 境内基金：净值到 END_DATE，1w 起点（END_DATE-7）处有更早一条净值
    _seed_fund(
        db_session,
        "110022",
        "基金A",
        "1000.0000",
        [
            (END_DATE - timedelta(days=8), "0.9000", None),
            (END_DATE, "1.1000", None),
        ],
    )
    # QDII：最新净值滞后 5 天
    qdii_end = END_DATE - timedelta(days=5)
    _seed_fund(
        db_session,
        "050025",
        "博时标普500ETF联接(QDII)A",
        "1000.0000",
        [
            (qdii_end - timedelta(days=10), "2.0000", None),
            (qdii_end, "2.1000", None),
        ],
    )

    data = client.get("/api/portfolio/returns", params={"window": "1w"}).json()
    window = data["windows"]["1w"]
    items = {item["instrument_code"]: item for item in window["items"]}
    qdii = items["050025"]
    assert qdii["is_qdii"] is True
    assert qdii["status"] == "available"
    # 使用自身最新净值日期，而非全局终点
    assert qdii["end_date"] == qdii_end.isoformat()
    # 1w 目标起点 = END_DATE - 7 = qdii_end - 2，<= 它的最后一条是 10 天前那条
    assert qdii["start_date"] == (qdii_end - timedelta(days=10)).isoformat()
    # 滞后说明
    assert "QDII" in qdii["stale_reason"]
    assert float(qdii["return_amount"]) == pytest.approx(100)
    # 境内基金无滞后说明
    assert items["110022"]["stale_reason"] is None
    # 组合 as_of 取参与加权基金的最大实际日期
    assert window["as_of_end_date"] == END_DATE.isoformat()
