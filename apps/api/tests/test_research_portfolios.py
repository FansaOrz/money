"""统一研究组合接口测试（GET /api/research/portfolios）。

覆盖：
1. 成功路径：最新候选池 + 研究就绪净值 → 200，基金组合 kind='fund'、
   status='research_only'，holdings 字段（code/name/weight/score/reason/
   reasons/market）齐全且权重/分数口径与 V2 信号一致；
2. 降级路径（均为 200，不返回 404）：
   - 尚未建任何候选池 → portfolios=[] + 顶层 warnings 说明；
   - 池内成员净值样本不足（研究就绪数据不足）→ portfolios=[] + warnings；
   - 股票研究组合数据链路未接入 → 顶层 warnings 固定提示，不伪造股票组合。

使用合成的确定性净值序列，不依赖外部行情。
"""

from datetime import date, timedelta
from decimal import Decimal

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models import CandidatePool, CandidatePoolMember, FundNav, Instrument


# ---------------------------------------------------------------------------
# 数据构造辅助（与 test_quant_discovery.py 同一套确定性口径）
# ---------------------------------------------------------------------------

BASE_DATE = date(2024, 1, 1)


def _seed_navs(
    db: Session,
    code: str,
    name: str,
    days: int = 800,
    daily_growth: float = 0.0005,
    noise: float = 0.001,
    start: date = BASE_DATE,
) -> Instrument:
    """构造确定性净值序列（unit_nav 与 accumulated_nav 一致）。"""
    instrument = Instrument(code=code, name=name)
    db.add(instrument)
    db.flush()
    nav = 1.0
    for i in range(days):
        db.add(
            FundNav(
                instrument_id=instrument.id,
                nav_date=start + timedelta(days=i),
                unit_nav=Decimal(f"{nav:.6f}"),
                accumulated_nav=Decimal(f"{nav:.6f}"),
                source="test",
            )
        )
        jitter = noise if i % 2 == 0 else -noise
        nav *= 1 + daily_growth + jitter
    db.commit()
    return instrument


def _seed_pool(db: Session, codes: list[str], name: str = "测试池") -> CandidatePool:
    """mock 候选池：CandidatePool + active 成员（rank 按传入顺序）。"""
    pool = CandidatePool(name=name, member_count=len(codes))
    db.add(pool)
    db.flush()
    for rank, code in enumerate(codes, start=1):
        db.add(
            CandidatePoolMember(
                pool_id=pool.id,
                code=code,
                name=f"基金{code}",
                rank=rank,
                status="active",
            )
        )
    db.commit()
    return pool


# ---------------------------------------------------------------------------
# 成功路径
# ---------------------------------------------------------------------------


def test_research_portfolios_success(client: TestClient, db_session: Session) -> None:
    """最新候选池 + 就绪净值 → 200 + 基金组合（前端兼容字段齐全）。"""
    codes = ["RP01", "RP02", "RP03"]
    names = ["易方达消费行业股票", "华夏中证红利指数", "博时沪深300联接"]
    growths = [0.002, 0.001, 0.0015]
    for code, name, growth in zip(codes, names, growths, strict=True):
        _seed_navs(db_session, code, name, daily_growth=growth)
    pool = _seed_pool(db_session, codes, name="研究组合测试池")

    response = client.get("/api/research/portfolios")
    assert response.status_code == 200
    payload = response.json()

    # 顶层结构：portfolios / as_of / warnings（前端 ResearchPortfoliosResponse 兼容）
    assert "portfolios" in payload
    assert "warnings" in payload
    assert payload["as_of"]

    assert len(payload["portfolios"]) == 1
    portfolio = payload["portfolios"][0]
    assert portfolio["kind"] == "fund"
    assert portfolio["status"] == "research_only"
    assert portfolio["id"] == f"fund-v2-pool-{pool.id}"
    assert "研究组合测试池" in portfolio["name"]
    assert portfolio["description"]
    assert portfolio["methodology"]
    assert portfolio["as_of"] == payload["as_of"]

    # 全部为确定性上行序列：动量 > 0，应有入选持仓
    holdings = portfolio["holdings"]
    assert holdings
    total_weight = 0.0
    for holding in holdings:
        for field in ("code", "name", "weight", "score", "reason", "reasons", "market"):
            assert field in holding
        assert holding["code"] in codes
        assert holding["weight"] > 0
        assert holding["score"] is not None and holding["score"] > 0
        assert isinstance(holding["reasons"], list) and holding["reasons"]
        assert holding["reason"]  # reason 为 reasons 的拼接摘要
        total_weight += holding["weight"]
    assert 0 < total_weight <= 1.0 + 1e-9

    # 股票侧固定提示，不伪造股票组合
    assert all(p["kind"] != "stock" for p in payload["portfolios"])
    assert any("股票研究组合暂不可用" in w for w in payload["warnings"])


def test_research_portfolios_picks_latest_pool(
    client: TestClient, db_session: Session
) -> None:
    """存在多个候选池时选取最新（创建时间倒序）的池。"""
    _seed_navs(db_session, "OLD1", "易方达消费行业股票", daily_growth=0.001)
    _seed_navs(db_session, "OLD2", "华夏中证红利指数", daily_growth=0.0012)
    old_pool = _seed_pool(db_session, ["OLD1", "OLD2"], name="旧池")
    _seed_navs(db_session, "NEW1", "易方达消费行业股票", daily_growth=0.001)
    _seed_navs(db_session, "NEW2", "华夏中证红利指数", daily_growth=0.0012)
    new_pool = _seed_pool(db_session, ["NEW1", "NEW2"], name="新池")

    response = client.get("/api/research/portfolios")
    assert response.status_code == 200
    payload = response.json()
    assert len(payload["portfolios"]) == 1
    portfolio = payload["portfolios"][0]
    # 两个池先后创建，最新池 id 更大且被选中
    assert new_pool.id > old_pool.id
    assert portfolio["id"] == f"fund-v2-pool-{new_pool.id}"
    assert "新池" in portfolio["name"]
    holding_codes = {h["code"] for h in portfolio["holdings"]}
    assert holding_codes <= {"NEW1", "NEW2"}


# ---------------------------------------------------------------------------
# 降级路径（数据不足仍返回 200）
# ---------------------------------------------------------------------------


def test_research_portfolios_no_pool(client: TestClient) -> None:
    """尚未建任何候选池：200 + portfolios=[] + 顶层 warnings 说明（不返回 404）。"""
    response = client.get("/api/research/portfolios")
    assert response.status_code == 200
    payload = response.json()
    assert payload["portfolios"] == []
    assert payload["as_of"] is None
    assert any("尚无候选池" in w for w in payload["warnings"])
    assert any("股票研究组合暂不可用" in w for w in payload["warnings"])


def test_research_portfolios_insufficient_nav_data(
    client: TestClient, db_session: Session
) -> None:
    """池内成员净值样本不足（研究就绪数据不足）：200 + portfolios=[] + warnings。"""
    # 仅 30 条净值，远低于 253 的研究样本门槛
    _seed_navs(db_session, "LOW1", "易方达新基金A", days=30)
    _seed_navs(db_session, "LOW2", "华夏新基金B", days=30)
    pool = _seed_pool(db_session, ["LOW1", "LOW2"], name="低样本池")

    response = client.get("/api/research/portfolios")
    assert response.status_code == 200
    payload = response.json()
    assert payload["portfolios"] == []
    assert payload["as_of"] is None
    assert any(f"候选池 #{pool.id}" in w for w in payload["warnings"])
    assert any("研究就绪" in w or "样本不足" in w for w in payload["warnings"])
    assert any("股票研究组合暂不可用" in w for w in payload["warnings"])
