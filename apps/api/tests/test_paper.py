"""模拟交易（Paper Trading）测试：每日循环、调仓成交、估值、基准、幂等与 API。

使用合成的确定性净值序列（线性趋势），候选池通过真实持仓（Position）提供，
保证 screener 候选充足（≥10 只时五档排名生效）；断言以业务不变量为主，
不依赖具体入选名单。
"""

from datetime import date, timedelta
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    Account,
    BacktestRun,
    FundNav,
    Instrument,
    PaperAccount,
    PaperHoldingDaily,
    PaperNavDaily,
    PaperPosition,
    PaperTrade,
    Position,
    SignalSnapshot,
    StrategyVersion,
)
from app.services import paper as paper_service
from app.services.paper import PaperError

BASE = date(2025, 1, 6)
CANDIDATE_COUNT = 12  # ≥10，五档排名正常生效
TARGET_CODES = ["100001", "100002"]  # 综合分前两名，多轮再分配后权重最大且近似相等


# ---------------------------------------------------------------------------
# 数据构造辅助
# ---------------------------------------------------------------------------


def _seed_fund(
    db: Session,
    code: str,
    name: str,
    days: int = 130,
    start_nav: float = 1.0,
    daily_growth: float = 0.001,
) -> Instrument:
    """写入一只基金及带交替噪声的线性净值序列（与 screener 测试同构）。"""
    instrument = Instrument(code=code, name=name)
    db.add(instrument)
    db.flush()
    nav = start_nav
    for i in range(days):
        db.add(
            FundNav(
                instrument_id=instrument.id,
                nav_date=BASE + timedelta(days=i),
                unit_nav=Decimal(f"{nav:.6f}"),
                accumulated_nav=Decimal(f"{nav:.6f}"),
                source="test",
            )
        )
        noise = 0.001 if i % 2 == 0 else -0.001
        nav *= 1 + daily_growth + noise
    db.commit()
    return instrument


def _seed_position(db: Session, instrument: Instrument) -> None:
    account = Account(name=f"账户-{instrument.code}")
    db.add(account)
    db.flush()
    db.add(
        Position(
            account_id=account.id,
            instrument_id=instrument.id,
            shares=Decimal("100"),
            cost=Decimal("10000.00"),
        )
    )
    db.commit()


def _seed_candidate_pool(db: Session, days: int = 130) -> dict[str, Instrument]:
    """构造 12 只候选基金并全部挂到真实持仓（screener 默认候选池）。

    12 只全属同一市场（A股），单一市场 50% 上限先触顶；多轮再分配后
    前两名（增速最高、综合分稳居前二）权重最大且近似相等，其后成员
    按综合分占比分享剩余市场额度（目标权重 > 0 的共 4 只）。
    """
    pool: dict[str, Instrument] = {}
    growths = [0.004, 0.004] + [0.0015 - i * 0.0001 for i in range(CANDIDATE_COUNT - 2)]
    for i in range(CANDIDATE_COUNT):
        code = TARGET_CODES[i] if i < 2 else f"1001{i:02d}"
        instrument = _seed_fund(
            db, code, f"测试股票基金{i:02d}", days=days, daily_growth=growths[i]
        )
        _seed_position(db, instrument)
        pool[code] = instrument
    return pool


def _run_days(db: Session, days: list[date]) -> list:
    return [paper_service.run_paper_cycle(db, run_date=d) for d in days]


# ---------------------------------------------------------------------------
# 账户初始化
# ---------------------------------------------------------------------------


def test_ensure_default_account_creates_with_defaults(db_session: Session) -> None:
    account = paper_service.ensure_default_account(db_session)
    assert account.cash == Decimal("1000000")
    assert account.initial_capital == Decimal("1000000")
    version = db_session.get(StrategyVersion, account.strategy_version_id)
    assert version.rebalance_interval == 20
    assert Decimal(version.fee_rate) == Decimal("0.001")
    assert version.top_n == 10

    # 重复调用不重复创建
    again = paper_service.ensure_default_account(db_session)
    assert again.id == account.id
    assert db_session.execute(select(PaperAccount)).scalars().all().__len__() == 1


def test_superseded_strategy_cannot_run(db_session: Session) -> None:
    version = StrategyVersion(
        name=paper_service.DEFAULT_STRATEGY_NAME,
        initial_capital=Decimal("1000000"),
        rebalance_interval=20,
        fee_rate=Decimal("0.001"),
        top_n=10,
        status="superseded_invalid_methodology",
        params={"superseded_reason": "旧方法失效"},
    )
    db_session.add(version)
    db_session.commit()
    with pytest.raises(PaperError, match="旧方法失效"):
        paper_service.ensure_default_account(db_session)


def test_summary_without_account_returns_404(client: TestClient) -> None:
    response = client.get("/api/paper/summary")
    assert response.status_code == 404


def test_run_without_candidates_returns_400(client: TestClient) -> None:
    """无任何持仓基金时 screener 抛错，run 返回 400 且不产生运行记录。"""
    response = client.post("/api/paper/run", json={"run_date": "2025-06-30"})
    assert response.status_code == 400
    assert "筛选器" in response.json()["detail"]


# ---------------------------------------------------------------------------
# 首次运行 = 建仓日
# ---------------------------------------------------------------------------


def test_first_run_is_initial_rebalance(db_session: Session) -> None:
    pool = _seed_candidate_pool(db_session)
    run_date = BASE + timedelta(days=129)  # 最后一天，全部净值可得
    result = paper_service.run_paper_cycle(db_session, run_date=run_date)

    assert result.rebalanced is True
    assert result.trading_day_index == 1
    assert result.skipped is False
    # 单一市场 50% 上限先触顶：多轮再分配后 7 只获得非零目标权重（合计 50%）
    assert result.trade_count == 7

    account = paper_service.get_default_account(db_session)
    positions = db_session.execute(
        select(PaperPosition).where(
            PaperPosition.account_id == account.id, PaperPosition.shares > 0
        )
    ).scalars().all()
    assert len(positions) == 7
    held_ids = {p.instrument_id for p in positions}
    # 前两名（TARGET_CODES）权重最大且必须入选
    assert {pool[code].id for code in TARGET_CODES} <= held_ids

    trades = db_session.execute(
        select(PaperTrade).where(PaperTrade.account_id == account.id)
    ).scalars().all()
    assert all(t.side == "buy" for t in trades)
    # 目标权重合计 = 市场顶 50%（截断部分保留为现金）
    total_target = sum(Decimal(t.target_weight) for t in trades)
    assert total_target == pytest.approx(Decimal("0.50"), abs=Decimal("0.001"))
    # 前两名权重最大且近似相等（多轮再分配同比例收敛）
    weight_by_code = {}
    for trade in trades:
        instrument = db_session.get(Instrument, trade.instrument_id)
        weight_by_code[instrument.code] = Decimal(trade.target_weight)
    top_weights = [weight_by_code[code] for code in TARGET_CODES]
    assert top_weights[0] == pytest.approx(top_weights[1], abs=Decimal("0.001"))
    assert all(top_weights[0] >= w for c, w in weight_by_code.items())

    # 现金 = 100 万 - 买入总额(50%) - 费用（0.1%×买入额）
    assert Decimal("499000.00") < account.cash < Decimal("500000.00")

    # 信号快照已固化，包含全部 12 只候选
    snapshots = db_session.execute(select(SignalSnapshot)).scalars().all()
    assert len(snapshots) == 1
    assert snapshots[0].run_id is not None
    assert len(snapshots[0].items) == CANDIDATE_COUNT
    assert snapshots[0].candidate_count == CANDIDATE_COUNT

    # 当日估值：总市值 = 现金 + 持仓市值（合计 50 万）；净值落库
    nav_row = db_session.execute(
        select(PaperNavDaily).where(PaperNavDaily.account_id == account.id)
    ).scalars().one()
    assert nav_row.rebalanced is True
    assert Decimal(nav_row.market_value) == Decimal("500000.00")
    # 费用 = 0.1% × 买入总额（50 万）= 500，体现为净值亏损
    assert Decimal(nav_row.fee_total) == Decimal("500.00")
    assert Decimal(nav_row.total_value) == Decimal("999500.00")
    assert nav_row.benchmark_nav is not None
    assert Decimal(nav_row.benchmark_nav) == Decimal("1.000000")  # 基准起点

    holdings = db_session.execute(
        select(PaperHoldingDaily).where(PaperHoldingDaily.account_id == account.id)
    ).scalars().all()
    assert len(holdings) == 7
    weight_sum = sum(Decimal(h.weight) for h in holdings)
    assert abs(float(weight_sum) - 500000.0 / 999500.0) < 1e-4


def test_non_rebalance_day_only_values(db_session: Session) -> None:
    _seed_candidate_pool(db_session)
    days = [BASE + timedelta(days=129 - i) for i in range(2, -1, -1)]  # 连续三天
    results = _run_days(db_session, days)

    assert results[0].rebalanced is True
    assert results[1].rebalanced is False
    assert results[2].rebalanced is False
    assert results[1].trade_count == 0

    account = paper_service.get_default_account(db_session)
    nav_rows = db_session.execute(
        select(PaperNavDaily)
        .where(PaperNavDaily.account_id == account.id)
        .order_by(PaperNavDaily.nav_date)
    ).scalars().all()
    assert len(nav_rows) == 3
    # 非调仓日现金不变，市值随净值上涨，日收益为正
    assert Decimal(nav_rows[1].cash) == Decimal(nav_rows[0].cash)
    assert Decimal(nav_rows[1].total_value) > Decimal(nav_rows[0].total_value)
    assert nav_rows[1].daily_return is not None
    assert float(nav_rows[1].daily_return) > 0
    # 快照仍只有建仓日一次
    assert db_session.execute(select(SignalSnapshot)).scalars().all().__len__() == 1
    # 持仓每日快照逐日记录
    holdings = db_session.execute(
        select(PaperHoldingDaily).where(PaperHoldingDaily.account_id == account.id)
    ).scalars().all()
    assert len(holdings) == 21  # 7 只 × 3 天


# ---------------------------------------------------------------------------
# 月调仓节奏：第 21 个交易日第二次调仓
# ---------------------------------------------------------------------------


def test_second_rebalance_on_day_21(db_session: Session) -> None:
    _seed_candidate_pool(db_session)
    last = BASE + timedelta(days=129)
    days = [last - timedelta(days=i) for i in range(24, -1, -1)]  # 25 个连续日
    results = _run_days(db_session, days)

    rebalanced = [r for r in results if r.rebalanced]
    assert [r.trading_day_index for r in rebalanced] == [1, 21]
    second = rebalanced[1]
    # 第二次调仓：先全卖 7 只持仓，再按目标买回（目标池未变）
    second_buys = 0
    second_sells = 0

    account = paper_service.get_default_account(db_session)
    trades = db_session.execute(
        select(PaperTrade)
        .where(PaperTrade.account_id == account.id, PaperTrade.run_id is not None)
        .order_by(PaperTrade.id)
    ).scalars().all()
    second_run_trades = [t for t in trades if t.trade_date == date.fromisoformat(second.run_date)]
    second_sells = sum(1 for t in second_run_trades if t.side == "sell")
    second_buys = sum(1 for t in second_run_trades if t.side == "buy")
    assert second.trade_count == second_sells + second_buys
    assert second_sells == 7  # 全部持仓先卖出
    assert second_buys >= 1  # 目标权重 > 0 的基金买回

    # 资金守恒：每次估值 total = cash + market_value
    nav_rows = db_session.execute(
        select(PaperNavDaily)
        .where(PaperNavDaily.account_id == account.id)
        .order_by(PaperNavDaily.nav_date)
    ).scalars().all()
    for row in nav_rows:
        assert Decimal(row.cash) + Decimal(row.market_value) == Decimal(row.total_value)

    snapshots = db_session.execute(select(SignalSnapshot)).scalars().all()
    assert len(snapshots) == 2


def test_rebalance_sells_dropped_position(db_session: Session) -> None:
    """第一次建仓后人为修改目标池：第二次调仓应卖出不在目标内的持仓。"""
    pool = _seed_candidate_pool(db_session)
    last = BASE + timedelta(days=129)
    first_day = last - timedelta(days=24)
    paper_service.run_paper_cycle(db_session, run_date=first_day)

    account = paper_service.get_default_account(db_session)
    # 人为把一只非目标基金塞入持仓（模拟目标池变化前的旧持仓）
    stranger = pool["100110"]
    db_session.add(
        PaperPosition(
            account_id=account.id,
            instrument_id=stranger.id,
            shares=Decimal("1000"),
            cost=Decimal("1500.00"),
        )
    )
    db_session.commit()

    days = [first_day + timedelta(days=i) for i in range(1, 21)]
    results = _run_days(db_session, days)
    assert results[-1].rebalanced is True

    stranger_position = db_session.execute(
        select(PaperPosition).where(
            PaperPosition.account_id == account.id,
            PaperPosition.instrument_id == stranger.id,
        )
    ).scalars().one()
    assert stranger_position.shares == Decimal("0")  # 已被全卖出

    sell_trades = db_session.execute(
        select(PaperTrade).where(
            PaperTrade.account_id == account.id,
            PaperTrade.instrument_id == stranger.id,
            PaperTrade.side == "sell",
        )
    ).scalars().all()
    assert len(sell_trades) == 1
    assert Decimal(sell_trades[0].fee) > 0


# ---------------------------------------------------------------------------
# 幂等
# ---------------------------------------------------------------------------


def test_run_is_idempotent(db_session: Session) -> None:
    _seed_candidate_pool(db_session)
    run_date = BASE + timedelta(days=129)
    first = paper_service.run_paper_cycle(db_session, run_date=run_date)
    second = paper_service.run_paper_cycle(db_session, run_date=run_date)

    assert second.skipped is True
    assert second.total_value == first.total_value
    assert second.trade_count == first.trade_count

    account = paper_service.get_default_account(db_session)
    assert db_session.execute(
        select(BacktestRun).where(BacktestRun.account_id == account.id)
    ).scalars().all().__len__() == 1
    assert db_session.execute(
        select(PaperTrade).where(PaperTrade.account_id == account.id)
    ).scalars().all().__len__() == 7
    assert db_session.execute(
        select(PaperNavDaily).where(PaperNavDaily.account_id == account.id)
    ).scalars().all().__len__() == 1
    assert db_session.execute(select(SignalSnapshot)).scalars().all().__len__() == 1

    # 账户现金未被重复扣减（建仓买入 50% + 费用后剩余约 49.95 万）
    assert Decimal("499000.00") < account.cash < Decimal("500000.00")


# ---------------------------------------------------------------------------
# 基准
# ---------------------------------------------------------------------------


def test_benchmark_equal_weight_tracks_candidates(db_session: Session) -> None:
    pool = _seed_candidate_pool(db_session, days=130)
    last = BASE + timedelta(days=129)
    days = [last - timedelta(days=i) for i in range(4, -1, -1)]
    _run_days(db_session, days)

    account = paper_service.get_default_account(db_session)
    nav_rows = db_session.execute(
        select(PaperNavDaily)
        .where(PaperNavDaily.account_id == account.id)
        .order_by(PaperNavDaily.nav_date)
    ).scalars().all()
    assert Decimal(nav_rows[0].benchmark_nav) == Decimal("1.000000")

    # 手工核算最后一天的等权基准：12 只候选日收益均值（构造数据每日收益恒定）
    expected_daily: list[Decimal] = []
    for code, instrument in pool.items():
        navs = db_session.execute(
            select(FundNav)
            .where(FundNav.instrument_id == instrument.id)
            .order_by(FundNav.nav_date)
        ).scalars().all()
        by_date = {n.nav_date: Decimal(n.accumulated_nav) for n in navs}
        ratios = [
            by_date[d2] / by_date[d1] - 1 for d1, d2 in zip(days, days[1:])
        ]
        expected_daily.append(ratios)
    for day_index in range(1, len(days)):
        avg = sum(r[day_index - 1] for r in expected_daily) / len(expected_daily)
        prev = Decimal(nav_rows[day_index - 1].benchmark_nav)
        expected = prev * (1 + avg)
        actual = Decimal(nav_rows[day_index].benchmark_nav)
        assert abs(float(actual - expected)) < 1e-5


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------


@pytest.fixture()
def seeded_client(client: TestClient, db_session: Session) -> TestClient:
    _seed_candidate_pool(db_session)
    last = BASE + timedelta(days=129)
    days = [last - timedelta(days=i) for i in range(21, -1, -1)]  # 22 天（两次调仓）
    _run_days(db_session, days)
    return client


def test_api_summary(seeded_client: TestClient) -> None:
    data = seeded_client.get("/api/paper/summary").json()
    assert Decimal(data["strategy"]["initial_capital"]) == Decimal("1000000")
    assert data["strategy"]["rebalance_interval"] == 20
    assert data["strategy"]["fee_rate"] == pytest.approx(0.001)
    assert data["position_count"] == 7
    assert data["rebalance_count"] == 2
    # 建仓 7 买 + 二次调仓 7 卖 7 买
    assert data["trade_count"] == 21
    assert Decimal(data["total_fees"]) > 0
    assert data["nav"] is not None
    assert data["benchmark_nav"] is not None
    assert data["excess_return"] is not None
    assert data["next_rebalance_in"] == 19  # 第 22 天刚运行完，距第 41 天调仓 19 天


def test_api_history(seeded_client: TestClient) -> None:
    data = seeded_client.get("/api/paper/history").json()
    assert data["count"] == 22
    first, last_point = data["items"][0], data["items"][-1]
    assert first["rebalanced"] is True
    assert first["nav"] == pytest.approx(0.9995, abs=1e-6)
    assert first["benchmark_nav"] == pytest.approx(1.0)
    assert last_point["daily_return"] is not None
    assert last_point["cumulative_return"] == pytest.approx(last_point["nav"] - 1, abs=1e-6)
    rebalance_points = [p for p in data["items"] if p["rebalanced"]]
    assert len(rebalance_points) == 2

    # 区间过滤
    start = data["items"][5]["date"]
    filtered = seeded_client.get(
        "/api/paper/history", params={"start_date": start}
    ).json()
    assert filtered["count"] == 22 - 5
    assert filtered["start_date"] == start


def test_api_positions(seeded_client: TestClient) -> None:
    data = seeded_client.get("/api/paper/positions").json()
    assert data["count"] == 7
    codes = {item["code"] for item in data["items"]}
    # 前两名（综合分最强）必须入选且权重最大
    assert set(TARGET_CODES) <= codes
    weight_by_code = {item["code"]: item["weight"] for item in data["items"]}
    top_weight = max(weight_by_code[code] for code in TARGET_CODES)
    assert all(weight_by_code[code] <= top_weight + 1e-9 for code in weight_by_code)
    for item in data["items"]:
        assert Decimal(item["market_value"]) > 0
        assert item["profit"] is not None
    total = Decimal(data["total_value"])
    expected = Decimal(data["cash"]) + sum(Decimal(i["market_value"]) for i in data["items"])
    assert float(total) == pytest.approx(float(expected))


def test_api_trades(seeded_client: TestClient) -> None:
    data = seeded_client.get("/api/paper/trades").json()
    assert data["count"] == 21
    assert len(data["items"]) == 21
    # 日期倒序
    dates = [item["date"] for item in data["items"]]
    assert dates == sorted(dates, reverse=True)
    sides = {item["side"] for item in data["items"]}
    assert sides == {"buy", "sell"}
    for item in data["items"]:
        assert Decimal(item["fee"]) > 0
        assert float(Decimal(item["amount"])) == pytest.approx(
            float(Decimal(item["shares"]) * Decimal(item["price"])), abs=0.01
        )

    # 分页
    page = seeded_client.get("/api/paper/trades", params={"limit": 2, "offset": 4}).json()
    assert page["count"] == 21
    assert len(page["items"]) == 2


def test_api_signals(seeded_client: TestClient) -> None:
    data = seeded_client.get("/api/paper/signals").json()
    assert data["count"] == 2  # 两次调仓各固化一次
    latest = data["items"][0]
    assert latest["candidate_count"] == CANDIDATE_COUNT
    assert len(latest["items"]) == CANDIDATE_COUNT
    target_items = [i for i in latest["items"] if i["target_weight"] > 0]
    # 前两名（综合分最强）必须入选且权重最大
    assert set(TARGET_CODES) <= {i["code"] for i in target_items}
    top_weight = max(
        i["target_weight"] for i in target_items if i["code"] in TARGET_CODES
    )
    assert all(i["target_weight"] <= top_weight + 1e-9 for i in target_items)
    # 多轮再分配：目标权重合计 = 市场顶 50%，单基金 ≤25%
    assert sum(i["target_weight"] for i in target_items) == pytest.approx(0.50, abs=1e-3)
    assert all(i["target_weight"] <= 0.25 + 1e-9 for i in target_items)
    assert "规则模型" in latest["methodology"]


def test_api_run_endpoint_and_idempotency(client: TestClient, db_session: Session) -> None:
    _seed_candidate_pool(db_session)
    run_date = (BASE + timedelta(days=129)).isoformat()
    first = client.post("/api/paper/run", json={"run_date": run_date})
    assert first.status_code == 200
    body = first.json()
    assert body["rebalanced"] is True
    assert body["trade_count"] == 7
    assert body["skipped"] is False
    assert body["total_value"] == "999500.00"

    second = client.post("/api/paper/run", json={"run_date": run_date})
    assert second.json()["skipped"] is True

    # 空 body 也可运行（使用当日日期，无新净值则估值不变）
    third = client.post("/api/paper/run")
    assert third.status_code == 200
    assert third.json()["rebalanced"] is False  # 第 2 个交易日，非调仓日


def test_api_run_invalid_date(client: TestClient, db_session: Session) -> None:
    _seed_candidate_pool(db_session)
    response = client.post("/api/paper/run", json={"run_date": "2025/06/30"})
    assert response.status_code == 422


def test_api_holdings_history(seeded_client: TestClient) -> None:
    data = seeded_client.get("/api/paper/holdings").json()
    assert len(data) == 154  # 7 只 × 22 天
    first_day = data[0]["date"]
    same_day = [row for row in data if row["date"] == first_day]
    assert len(same_day) == 7
    weight_sum = sum(row["weight"] for row in same_day)
    assert 0 < weight_sum <= 1


# ---------------------------------------------------------------------------
# 服务层边界
# ---------------------------------------------------------------------------


def test_get_summary_metrics_after_history(db_session: Session) -> None:
    _seed_candidate_pool(db_session)
    last = BASE + timedelta(days=129)
    days = [last - timedelta(days=i) for i in range(9, -1, -1)]
    _run_days(db_session, days)

    summary = paper_service.get_summary(db_session)
    assert summary.total_return is not None
    assert summary.annual_return is not None
    assert summary.max_drawdown is not None
    assert summary.sharpe is not None
    assert summary.start_date == days[0].isoformat()
    assert summary.last_run_date == days[-1].isoformat()


def test_get_default_account_raises_when_missing(db_session: Session) -> None:
    with pytest.raises(PaperError):
        paper_service.get_default_account(db_session)


def test_history_rejects_inverted_range(db_session: Session) -> None:
    _seed_candidate_pool(db_session)
    paper_service.run_paper_cycle(db_session, run_date=BASE + timedelta(days=129))
    with pytest.raises(PaperError):
        paper_service.get_history(db_session, start_date="2025-07-01", end_date="2025-06-01")
