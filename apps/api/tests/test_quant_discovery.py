"""基金发现量化后端测试。

覆盖：
1. 因子榜：收益/波动/回撤/夏普/索提诺/Calmar/CVaR95/12-1 动量/同类分位
   字段齐全且数值正确；排序、升降序、limit/offset 分页与 total 口径；
2. 双动量：相对动量前 top_n 等权、绝对动量 ≤ 0 整体回避（hold_offense=false）；
3. V2 信号 / V2 回测：候选池 pool_id 解析（mock CandidatePool/Member +
   FundNav 净值），响应结构与 /api/quant/v2/* 一致且附带来源提示；
4. 量化验证：pool_id 入口端到端（样本外指标 + 预测有效性 + 稳健性）；
5. 错误路径：pool 不存在 / 空池 / 未知代码 → 400。

使用合成的确定性净值序列，不依赖外部行情。
"""

from datetime import date, timedelta
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models import CandidatePool, CandidatePoolMember, FundNav, Instrument
from app.schemas.discovery_quant import FactorBoardQuery
from app.services import quant_discovery as discovery
from app.services import quant_risk as risk
from app.services.quant import (
    _annual_volatility,
    _daily_returns,
    _max_drawdown,
    _sharpe,
)
from app.services import quant_stats as stats


# ---------------------------------------------------------------------------
# 数据构造辅助
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


def _seed_large_ready_pool(db: Session, size: int) -> CandidatePool:
    """构造大池元数据；量化引擎由调用测试按需 mock，避免生成海量净值。"""
    codes = [f"L{i:04d}" for i in range(size)]
    pool = CandidatePool(name=f"大池{size}", member_count=size)
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
                nav_samples=800,
                nav_ready=True,
            )
        )
    db.commit()
    return pool


# ---------------------------------------------------------------------------
# 因子榜
# ---------------------------------------------------------------------------


def test_factor_board_values_match_reference(db_session: Session) -> None:
    """因子数值与 quant/quant_stats/quant_risk 参考实现逐字段一致。"""
    days = 800
    instrument = _seed_navs(db_session, "110011", "易方达沪深300ETF联接A", days=days)

    response = discovery.factor_leaderboard(
        db_session, FactorBoardQuery(codes=["110011"], window=252, min_samples=60)
    )

    assert response.total == 1
    assert response.pool_size == 1
    assert response.excluded_count == 0
    item = response.items[0]
    assert item.code == "110011"
    assert item.rank == 1
    assert item.market == "cn_300"
    assert item.sample_count == days

    # 参考序列：从库内重新装载（与服务同一数据口径，避免 6 位小数存储的舍入漂移）
    from app.services.quant import _calendar_period_return, _load_dual_nav_series

    series = _load_dual_nav_series(db_session, instrument.id).total_series
    values = [v for _, v in series]
    assert len(values) == days

    assert item.return_1m == pytest.approx(_calendar_period_return(series, months=1))
    assert item.return_3m == pytest.approx(_calendar_period_return(series, months=3))
    assert item.return_1y == pytest.approx(_calendar_period_return(series, months=12))
    assert item.return_3y == pytest.approx(_calendar_period_return(series, months=36))
    assert item.momentum_12_1 == pytest.approx(risk.absolute_momentum_12_1(values))

    tail = values[-253:]
    returns = _daily_returns(tail)
    assert item.annual_volatility == pytest.approx(_annual_volatility(returns))
    assert item.max_drawdown == pytest.approx(_max_drawdown(tail))
    assert item.sharpe == pytest.approx(_sharpe(returns))
    assert item.cvar95 == pytest.approx(stats.cvar95(returns))
    total = tail[-1] / tail[0] - 1.0
    assert item.calmar == pytest.approx(stats.calmar_ratio(total, len(tail) - 1, item.max_drawdown))
    # 索提诺存在且与夏普同号（确定性上行序列）
    assert item.sortino is not None and item.sortino > 0
    # 同类分位：单只候选的市场层内分位数为 0
    assert item.quantile == 0.0


def test_factor_board_sort_and_pagination(db_session: Session) -> None:
    """排序（降序/升序）与分页：total 为分页前总数，rank 连续。"""
    # 三只基金增速递减 → 收益/动量排序确定
    _seed_navs(db_session, "F001", "华夏中证红利基金", daily_growth=0.003)
    _seed_navs(db_session, "F002", "易方达消费行业基金", daily_growth=0.001)
    _seed_navs(db_session, "F003", "博时沪深300基金", daily_growth=-0.001)

    desc = discovery.factor_leaderboard(
        db_session,
        FactorBoardQuery(codes=["F001", "F002", "F003"], sort="return_1y", order="desc"),
    )
    assert desc.total == 3
    assert [item.code for item in desc.items] == ["F001", "F002", "F003"]
    assert [item.rank for item in desc.items] == [1, 2, 3]

    asc = discovery.factor_leaderboard(
        db_session,
        FactorBoardQuery(codes=["F001", "F002", "F003"], sort="return_1y", order="asc"),
    )
    assert [item.code for item in asc.items] == ["F003", "F002", "F001"]

    page1 = discovery.factor_leaderboard(
        db_session,
        FactorBoardQuery(codes=["F001", "F002", "F003"], sort="return_1y", limit=2, offset=0),
    )
    page2 = discovery.factor_leaderboard(
        db_session,
        FactorBoardQuery(codes=["F001", "F002", "F003"], sort="return_1y", limit=2, offset=2),
    )
    assert page1.total == 3 and page2.total == 3
    assert [item.code for item in page1.items] == ["F001", "F002"]
    assert [item.code for item in page2.items] == ["F003"]
    # rank 跨页连续
    assert [item.rank for item in page1.items + page2.items] == [1, 2, 3]


def test_factor_board_quantile_within_market(db_session: Session) -> None:
    """同类分位：同市场层内按 12-1 动量取分位数，不跨市场比较。"""
    _seed_navs(db_session, "A001", "基金A 沪深300联接", daily_growth=0.002)
    _seed_navs(db_session, "A002", "基金B 沪深300联接", daily_growth=0.001)

    response = discovery.factor_leaderboard(
        db_session, FactorBoardQuery(codes=["A001", "A002"], sort="momentum_12_1")
    )
    by_code = {item.code: item for item in response.items}
    # 两只同层候选：强者分位 (2*1+1-1)/(2*2)=0.5，弱者 0.0
    assert by_code["A001"].quantile == pytest.approx(0.5)
    assert by_code["A002"].quantile == pytest.approx(0.0)
    assert by_code["A001"].momentum_12_1 > by_code["A002"].momentum_12_1


def test_factor_board_via_pool_id(client: TestClient, db_session: Session) -> None:
    """pool_id 入口：mock CandidatePool/Member + 净值，端到端分页因子榜。"""
    _seed_navs(db_session, "P001", "易方达沪深300联接A", daily_growth=0.002)
    _seed_navs(db_session, "P002", "华夏消费行业基金", daily_growth=0.001)
    pool = _seed_pool(db_session, ["P001", "P002"])

    response = client.get(
        f"/api/discovery/quant/factors?pool_id={pool.id}&sort=return_1y&limit=10"
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["total"] == 2
    assert payload["pool_size"] == 2
    assert [item["code"] for item in payload["items"]] == ["P001", "P002"]
    assert any(f"候选池 #{pool.id}" in w for w in payload["warnings"])
    assert payload["methodology"]
    first = payload["items"][0]
    for field in (
        "return_1m", "return_3m", "return_1y", "return_3y",
        "annual_volatility", "max_drawdown", "sharpe", "sortino",
        "calmar", "cvar95", "momentum_12_1", "quantile",
    ):
        assert field in first


def test_factor_board_excludes_insufficient_samples(db_session: Session) -> None:
    """样本不足的候选被剔除并计数；未知代码给出提示。"""
    _seed_navs(db_session, "S001", "易方达消费行业基金", days=800)
    _seed_navs(db_session, "S002", "华夏新基金", days=30)

    response = discovery.factor_leaderboard(
        db_session,
        FactorBoardQuery(codes=["S001", "S002", "UNKNOWN"], min_samples=60),
    )
    assert response.total == 1
    assert response.excluded_count == 1
    assert response.pool_size == 2
    assert response.items[0].code == "S001"
    assert any("未找到" in w for w in response.warnings)
    assert any("样本不足" in w for w in response.warnings)


# ---------------------------------------------------------------------------
# 双动量
# ---------------------------------------------------------------------------


def test_dual_momentum_relative_and_absolute(db_session: Session) -> None:
    """相对动量前 top_n 等权；绝对动量 ≤ 0 不入选。"""
    _seed_navs(db_session, "D001", "基金A 消费", daily_growth=0.002)
    _seed_navs(db_session, "D002", "基金B 红利", daily_growth=0.001)
    _seed_navs(db_session, "D003", "基金C 沪深300", daily_growth=-0.002)

    response = discovery.dual_momentum(
        db_session, discovery.DualMomentumQuery(codes=["D001", "D002", "D003"], top_n=2)
    )
    assert response.hold_offense is True
    by_code = {item.code: item for item in response.items}
    assert by_code["D001"].selected is True
    assert by_code["D002"].selected is True
    assert by_code["D003"].selected is False  # 动量 ≤ 0，绝对动量过滤
    assert by_code["D001"].weight == pytest.approx(0.5)
    assert by_code["D003"].weight == 0.0
    assert response.cash_weight == pytest.approx(0.0)


def test_dual_momentum_full_risk_off(db_session: Session) -> None:
    """前 top_n 全部动量 ≤ 0：整体回避，hold_offense=false、现金权重 1。"""
    _seed_navs(db_session, "E001", "基金A 消费", daily_growth=-0.002)
    _seed_navs(db_session, "E002", "基金B 红利", daily_growth=-0.001)

    response = discovery.dual_momentum(
        db_session, discovery.DualMomentumQuery(codes=["E001", "E002"], top_n=1)
    )
    assert response.hold_offense is False
    assert response.cash_weight == 1.0
    assert all(item.weight == 0.0 for item in response.items)
    assert any("整体回避" in w for w in response.warnings)


def test_dual_momentum_requires_momentum_samples(db_session: Session) -> None:
    """样本不足 253 的候选被跳过；全部不足时报 400。"""
    _seed_navs(db_session, "G001", "基金A 消费", days=100)

    with pytest.raises(discovery.QuantError, match="12-1 动量"):
        discovery.dual_momentum(
            db_session, discovery.DualMomentumQuery(codes=["G001"], top_n=1)
        )


# ---------------------------------------------------------------------------
# V2 信号 / V2 回测（pool_id 入口）
# ---------------------------------------------------------------------------


def test_signals_v2_via_pool_id(client: TestClient, db_session: Session) -> None:
    """pool_id 解析成员 → V2 当期信号（结构与 /api/quant/v2/signals 一致）。"""
    codes = ["V001", "V002", "V003"]
    names = ["易方达消费行业股票", "华夏中证红利指数", "博时沪深300联接"]
    growths = [0.002, 0.001, 0.0015]
    for code, name, growth in zip(codes, names, growths, strict=True):
        _seed_navs(db_session, code, name, daily_growth=growth)
    pool = _seed_pool(db_session, codes)

    response = client.get(f"/api/discovery/quant/signals-v2?pool_id={pool.id}&top_n=3")
    assert response.status_code == 200
    payload = response.json()
    assert payload["candidate_count"] == 3
    assert payload["as_of"]
    assert payload["methodology"]
    assert any(f"候选池 #{pool.id}" in w for w in payload["warnings"])
    # 全部为确定性上行序列：动量 > 0，应有入选与目标权重
    assert payload["selected"]
    total_weight = sum(item["weight"] for item in payload["selected"])
    assert 0 < total_weight <= 1.0 + 1e-9
    assert payload["cash_weight"] >= -1e-9


def test_backtest_v2_via_pool_id(client: TestClient, db_session: Session) -> None:
    """pool_id 解析成员 → V2 月频回测（结构与 /api/quant/v2/backtest 一致）。"""
    codes = ["B001", "B002", "B003"]
    names = ["易方达消费行业股票", "华夏中证红利指数", "博时沪深300联接"]
    for index, (code, name) in enumerate(zip(codes, names, strict=True)):
        _seed_navs(db_session, code, name, daily_growth=0.0008 + 0.0004 * index)
    pool = _seed_pool(db_session, codes)

    response = client.post(
        "/api/discovery/quant/backtest-v2",
        json={"pool_id": pool.id, "top_n": 3, "initial_capital": 10000},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["params"]["candidate_codes"] == codes
    assert payload["strategy"]["total_return"] is not None
    assert payload["benchmark"]["total_return"] is not None
    assert payload["rebalance_count"] >= 1
    assert payload["curve"]
    assert any(f"候选池 #{pool.id}" in w for w in payload["warnings"])
    # 权重约束：调仓明细中单基金 ≤ 8%（默认上限，允许浮点误差）
    for rebalance in payload["rebalances"]:
        for weight in rebalance["holdings"].values():
            assert weight <= 0.08 + 1e-6


def test_backtest_v2_explicit_codes_override_pool(
    client: TestClient, db_session: Session
) -> None:
    """显式 codes 优先于 pool_id。"""
    _seed_navs(db_session, "C001", "易方达消费行业股票", daily_growth=0.001)
    _seed_navs(db_session, "C002", "华夏中证红利指数", daily_growth=0.0012)
    pool = _seed_pool(db_session, ["C001", "C002"])

    response = client.post(
        "/api/discovery/quant/backtest-v2",
        json={"pool_id": pool.id, "codes": ["C001", "C002"], "top_n": 2},
    )
    assert response.status_code == 200
    assert response.json()["params"]["candidate_codes"] == ["C001", "C002"]
    # 显式 codes 路径不附池来源提示
    assert not any("候选池 #" in w for w in response.json()["warnings"])


# ---------------------------------------------------------------------------
# 量化验证（pool_id 入口）
# ---------------------------------------------------------------------------


def test_signals_v2_large_pool_passes_validated_codes(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """800 只研究就绪成员不会再触发旧的 250 长度校验。"""
    pool = _seed_large_ready_pool(db_session, 800)
    seen: dict[str, int] = {}

    def fake_signals(_db, req):
        from app.schemas.quant_v2 import SignalsV2Response

        seen["count"] = len(req.candidate_codes or [])
        return SignalsV2Response(
            as_of="2026-07-31",
            trade_date="2026-08-03",
            candidate_count=seen["count"],
            eligible_count=seen["count"],
            selected=[],
            cash_weight=1.0,
            vol_scalar=1.0,
            frozen=False,
        )

    monkeypatch.setattr(discovery.v2_service, "current_signals", fake_signals)
    response = discovery.pool_signals_v2(db_session, pool.id, None, 8)
    assert seen["count"] == 800
    assert response.candidate_count == 800


def test_validation_large_pool_is_transparently_capped(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """高成本统计验证最多取 250 只，并在 warnings 中明确说明。"""
    from app.schemas.quant import (
        ValidationCostSummary,
        ValidationNeighborhood,
        ValidationPredictiveness,
        ValidationRequest,
        ValidationResponse,
        ValidationRiskMetrics,
        ValidationRobustness,
    )

    pool = _seed_large_ready_pool(db_session, 800)
    seen: dict[str, int] = {}

    def fake_validation(_db, req):
        seen["count"] = len(req.candidate_codes or [])
        return ValidationResponse(
            as_of="2026-07-31",
            candidate_codes=req.candidate_codes or [],
            start_date="2025-01-01",
            end_date="2026-07-31",
            sample_count=300,
            oos_count=100,
            strategy=ValidationRiskMetrics(),
            benchmark=ValidationRiskMetrics(),
            predictiveness=ValidationPredictiveness(),
            robustness=ValidationRobustness(
                trial_count=1,
                bootstrap_resamples=0,
                block_length=0,
            ),
            neighborhood=ValidationNeighborhood(),
            costs=ValidationCostSummary(
                include_costs=True,
                buy_fee_rate=0.0015,
                sell_fee_rate=0.005,
                short_term_sell_fee_rate=0.015,
                short_term_days=7,
                total_fee_ratio=0.0,
                trade_days=0,
                sell_fee_basis="default",
            ),
        )

    monkeypatch.setattr(discovery.validation_service, "run_validation", fake_validation)
    result = discovery.pool_validation(db_session, pool.id, None, ValidationRequest())
    assert seen["count"] == 250
    assert any("最多使用 250" in warning for warning in result.warnings)


def test_validation_via_pool_id(client: TestClient, db_session: Session) -> None:
    """pool_id 解析成员 → 量化验证（结构与 /api/quant/validation 一致）。"""
    codes = ["W001", "W002", "W003"]
    names = ["易方达消费行业股票", "华夏中证红利指数", "博时沪深300联接"]
    for index, (code, name) in enumerate(zip(codes, names, strict=True)):
        _seed_navs(db_session, code, name, daily_growth=0.0006 + 0.0003 * index)
    pool = _seed_pool(db_session, codes)

    response = client.post(
        "/api/discovery/quant/validation",
        json={
            "pool_id": pool.id,
            "top_n": 2,
            "window": {"train_window": 120, "test_window": 20, "step": 20},
            "include_costs": False,
            "bootstrap_resamples": 100,
        },
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["candidate_codes"] == codes
    assert payload["oos_count"] > 0
    assert payload["strategy"]["total_return"] is not None
    assert payload["benchmark"]["total_return"] is not None
    assert "predictiveness" in payload and "robustness" in payload
    assert "neighborhood" in payload and "costs" in payload
    assert any(f"候选池 #{pool.id}" in w for w in payload["warnings"])


# ---------------------------------------------------------------------------
# 错误路径
# ---------------------------------------------------------------------------


def test_pool_not_found(client: TestClient) -> None:
    response = client.get("/api/discovery/quant/factors?pool_id=9999")
    assert response.status_code == 400
    assert "不存在" in response.json()["detail"]


def test_empty_pool(client: TestClient, db_session: Session) -> None:
    pool = CandidatePool(name="空池", member_count=0)
    db_session.add(pool)
    db_session.commit()

    response = client.get(f"/api/discovery/quant/factors?pool_id={pool.id}")
    assert response.status_code == 400
    assert "成员" in response.json()["detail"]


def test_excluded_members_ignored(db_session: Session) -> None:
    """status=excluded 的成员不参与候选解析。"""
    pool = CandidatePool(name="混合池", member_count=2)
    db_session.add(pool)
    db_session.flush()
    db_session.add(
        CandidatePoolMember(
            pool_id=pool.id, code="X001", name="基金X001", rank=1, status="excluded"
        )
    )
    db_session.add(
        CandidatePoolMember(
            pool_id=pool.id, code="X002", name="基金X002", rank=2, status="active"
        )
    )
    db_session.commit()

    assert discovery.resolve_pool_codes(db_session, pool.id) == ["X002"]


def test_unknown_codes_rejected(client: TestClient) -> None:
    response = client.get("/api/discovery/quant/factors?codes=NOPE1,NOPE2")
    assert response.status_code == 400
    assert "未找到" in response.json()["detail"]


def test_invalid_sort_factor_rejected(client: TestClient) -> None:
    response = client.get("/api/discovery/quant/factors?codes=A&sort=not_a_factor")
    assert response.status_code == 422


# ---------------------------------------------------------------------------
# 因子榜排序：缺失值（None）恒排末尾
# ---------------------------------------------------------------------------


def test_factor_board_none_sorts_last_in_desc(db_session: Session) -> None:
    """desc 排序时缺失因子值的候选恒在末尾（修复前 reverse 会把 None 排到最前）。"""
    _seed_navs(db_session, "N001", "华夏中证红利基金", daily_growth=0.003)
    _seed_navs(db_session, "N002", "易方达消费行业基金", daily_growth=0.001)
    # 恒定净值（日收益全 0）→ 日收益标准差 0 → 夏普为 None
    instrument = Instrument(code="N003", name="零波动债券基金")
    db_session.add(instrument)
    db_session.flush()
    base = date(2024, 1, 1)
    for i in range(800):
        db_session.add(
            FundNav(
                instrument_id=instrument.id,
                nav_date=base + timedelta(days=i),
                unit_nav=Decimal("1.000000"),
                accumulated_nav=Decimal("1.000000"),
                source="test",
            )
        )
    db_session.commit()

    codes = ["N001", "N002", "N003"]
    desc = discovery.factor_leaderboard(
        db_session,
        FactorBoardQuery(codes=codes, sort="sharpe", order="desc", min_samples=60),
    )
    sharpes = [item.sharpe for item in desc.items]
    assert sharpes[-1] is None, "desc 时 None 必须在末尾"
    assert all(v is not None for v in sharpes[:-1])
    assert [item.code for item in desc.items][-1] == "N003"

    asc = discovery.factor_leaderboard(
        db_session,
        FactorBoardQuery(codes=codes, sort="sharpe", order="asc", min_samples=60),
    )
    asc_sharpes = [item.sharpe for item in asc.items]
    assert asc_sharpes[-1] is None, "asc 时 None 也必须在末尾"


def test_methodology_declares_survivorship_bias() -> None:
    """因子榜与双动量方法论均声明当前候选池的幸存者偏差。"""
    assert "幸存者偏差" in discovery.METHODOLOGY_FACTORS
    assert "幸存者偏差" in discovery.METHODOLOGY_DUAL_MOMENTUM
