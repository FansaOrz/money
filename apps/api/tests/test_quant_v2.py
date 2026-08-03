"""稳健组合 V2 测试。

纯函数部分（run_backtest_panels / select_candidates / monthly_signal_indices）：
1. 无未来数据：篡改信号日之后的净值不改变任何调仓决策；
2. 月频信号日：每月最后一个交易日、严格递增；
3. T+1/T+2 成交：纯 A 股目标 T+1、含 QDII 目标 T+2；
4. 冻结：高波动+急反弹时沿用上一期持仓、换手为零；
5. 权重约束在回测持仓中始终成立（单基金 ≤8%、家族 ≤10%、QDII ≤30%）。

集成部分（TestClient + 临时 SQLite）：
6. POST /api/quant/v2/backtest 端到端：指标、曲线、调仓明细、T+1/T+2 成交记录；
7. GET /api/quant/v2/signals 端到端：当期信号、权重约束、费用模型默认值；
8. 费用模型：非零费率时费用计入且净值低于零费用基线；
9. 错误路径：候选不足 / 样本不足 → 400。

使用合成的确定性净值序列，不依赖外部行情。
"""

from datetime import date, timedelta
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models import Account, FundNav, Instrument, Position
from app.schemas.quant_v2 import BacktestV2Request, FeeModelConfig
from app.services import quant_risk as risk
from app.services import quant_v2 as v2


# ---------------------------------------------------------------------------
# 数据构造辅助
# ---------------------------------------------------------------------------


def _make_calendar(days: int, start: date = date(2024, 1, 1)) -> list[date]:
    return [start + timedelta(days=i) for i in range(days)]


def _trend_panel(days: int, daily: float, noise: float = 0.001) -> list[float]:
    """确定性趋势净值（交替噪声），daily 可为负。"""
    values = [1.0]
    for i in range(days - 1):
        jitter = noise if i % 2 == 0 else -noise
        values.append(values[-1] * (1 + daily + jitter))
    return values


def _crash_rebound_panel(days: int, crash_at: int, crash_depth: float = 0.30,
                         rebound: float = 0.025, rebound_days: int = 10) -> list[float]:
    """构造 高波动 + 急反弹 探针：crash_at 处单日暴跌，随后强势反弹。"""
    values = [1.0]
    for i in range(days - 1):
        idx = i + 1
        if idx == crash_at:
            values.append(values[-1] * (1 - crash_depth))
        elif crash_at < idx <= crash_at + rebound_days:
            values.append(values[-1] * (1 + rebound))
        else:
            values.append(values[-1] * (1 + (0.0008 if i % 2 == 0 else -0.0008)))
    return values


def _seed_navs(
    db: Session,
    code: str,
    name: str,
    days: int = 400,
    daily_growth: float = 0.001,
    noise: float = 0.001,
    start: date = date(2024, 1, 1),
) -> Instrument:
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


def _seed_position(db: Session, instrument: Instrument, market_value: str = "10000.00") -> None:
    account = Account(name=f"账户-{instrument.code}")
    db.add(account)
    db.flush()
    db.add(
        Position(
            account_id=account.id,
            instrument_id=instrument.id,
            shares=Decimal("1000"),
            cost=Decimal(market_value),
            market_value=Decimal(market_value),
        )
    )
    db.commit()


# ---------------------------------------------------------------------------
# 月频信号日
# ---------------------------------------------------------------------------


def test_monthly_signal_indices_are_month_ends() -> None:
    calendar = _make_calendar(400)
    indices = v2.monthly_signal_indices(calendar, 252)
    assert indices == sorted(set(indices))
    assert indices[0] >= 252
    for idx in indices:
        day = calendar[idx]
        nxt = calendar[idx + 1]
        # 每月最后一个交易日：次月与当月不同
        assert (nxt.year, nxt.month) != (day.year, day.month)


# ---------------------------------------------------------------------------
# 选基：绝对动量过滤 + 家族去重 + 层内前 30%
# ---------------------------------------------------------------------------


def test_select_candidates_filters_non_positive_momentum() -> None:
    days = 300
    calendar = _make_calendar(days)
    _ = calendar
    panels = {
        "UP": _trend_panel(days, 0.001),
        "DOWN": _trend_panel(days, -0.001),
    }
    names = {"UP": "易方达消费行业股票", "DOWN": "华夏消费升级股票"}
    selected, _ = v2.select_candidates(panels, names, days - 1)
    codes = {item["code"] for item in selected}
    assert "UP" in codes
    assert "DOWN" not in codes  # 绝对动量 < 0 被过滤
    assert all(item["momentum"] > 0 for item in selected)


def test_select_candidates_dedupes_family() -> None:
    days = 300
    panels = {
        "A": _trend_panel(days, 0.0005),
        "C": _trend_panel(days, 0.0012),  # 同家族 C 份额动量更高
    }
    names = {"A": "易方达沪深300ETF联接A", "C": "易方达沪深300ETF联接C"}
    selected, warnings = v2.select_candidates(panels, names, days - 1)
    codes = {item["code"] for item in selected}
    assert codes == {"C"}  # 同家族只保留动量最高者
    assert any("去重" in w for w in warnings)


def test_select_candidates_top_30pct_within_market() -> None:
    days = 300
    panels = {f"F{i}": _trend_panel(days, 0.0002 * (i + 1)) for i in range(10)}
    # 全部归入 A 股层（不同家族，避免去重干扰）
    names = {f"F{i}": f"基金{i}号消费股票" for i in range(10)}
    selected, _ = v2.select_candidates(panels, names, days - 1)
    assert len(selected) == 3  # ceil(10 × 0.3)
    momenta = sorted(item["momentum"] for item in selected)
    all_momenta = sorted(
        risk.absolute_momentum_12_1(panels[f"F{i}"]) for i in range(10)
    )
    assert momenta == all_momenta[-3:]  # 前 3 强


# ---------------------------------------------------------------------------
# 无未来数据（纯函数）
# ---------------------------------------------------------------------------


def _base_panels(days: int = 380) -> dict[str, list[float]]:
    return {
        "CN1": _trend_panel(days, 0.0012),
        "CN2": _trend_panel(days, 0.0008),
        "CN3": _trend_panel(days, 0.0004),
        "CN4": _trend_panel(days, -0.0005),
    }


_BASE_NAMES = {
    "CN1": "易方达消费行业股票",
    "CN2": "华夏医药健康股票",
    "CN3": "嘉实新兴产业股票",
    "CN4": "招商大盘蓝筹股票",
}


def test_decisions_ignore_future_data() -> None:
    """篡改所有信号日之后的净值，调仓决策（目标权重）逐期完全不变。"""
    days = 380
    calendar = _make_calendar(days)
    panels = _base_panels(days)
    req = BacktestV2Request(candidate_codes=list(panels), top_n=3)

    _, _, rebalances_base, _, _ = v2.run_backtest_panels(calendar, panels, _BASE_NAMES, req)
    assert rebalances_base, "应至少产生一次调仓"

    # 篡改：最后一个信号日之后的全部净值 ×5（信号日不可见）
    last_signal = max(
        i for i, d in enumerate(calendar)
        if any(
            date.fromisoformat(r.signal_date) == d for r in rebalances_base
        )
    )
    tampered = {
        code: [v if i <= last_signal else v * 5.0 for i, v in enumerate(values)]
        for code, values in panels.items()
    }
    _, _, rebalances_tampered, _, _ = v2.run_backtest_panels(
        calendar, tampered, _BASE_NAMES, req
    )

    for before, after in zip(rebalances_base, rebalances_tampered):
        assert before.signal_date == after.signal_date
        assert before.fill_date == after.fill_date
        assert before.holdings == after.holdings
        assert before.frozen == after.frozen


def test_scoring_uses_only_signal_day_data() -> None:
    """在信号日之后加入暴跌，不改变该期入选结果（打分只看 t 及之前）。"""
    days = 300
    t = days - 10
    panels = _base_panels(days)
    selected_before, _ = v2.select_candidates(panels, _BASE_NAMES, t)
    tampered = {
        code: [v if i <= t else v * 0.2 for i, v in enumerate(values)]
        for code, values in panels.items()
    }
    selected_after, _ = v2.select_candidates(tampered, _BASE_NAMES, t)
    assert {i["code"] for i in selected_before} == {i["code"] for i in selected_after}
    for a, b in zip(selected_before, selected_after):
        assert a["momentum"] == pytest.approx(b["momentum"])


# ---------------------------------------------------------------------------
# T+1 / T+2 成交与权重约束（纯函数）
# ---------------------------------------------------------------------------


def test_t1_settlement_for_domestic_targets() -> None:
    days = 380
    calendar = _make_calendar(days)
    panels = _base_panels(days)  # 全部 A 股
    req = BacktestV2Request(candidate_codes=list(panels), top_n=3)
    _, _, rebalances, trades, _ = v2.run_backtest_panels(calendar, panels, _BASE_NAMES, req)
    assert rebalances
    assert all(t.settle_lag == 1 for t in trades)
    for r in rebalances:
        if r.frozen:
            continue
        sig = date.fromisoformat(r.signal_date)
        fill = date.fromisoformat(r.fill_date)
        assert (fill - sig).days == 1  # T+1


def test_t2_settlement_when_qdii_in_target() -> None:
    days = 380
    calendar = _make_calendar(days)
    panels = {
        "CN1": _trend_panel(days, 0.0012),
        "CN2": _trend_panel(days, 0.0008),
        "US1": _trend_panel(days, 0.0015),  # QDII，动量最高
    }
    names = {
        "CN1": "易方达消费行业股票",
        "CN2": "华夏医药健康股票",
        "US1": "国泰纳斯达克100指数QDII",
    }
    req = BacktestV2Request(candidate_codes=list(panels), top_n=3)
    _, _, rebalances, trades, _ = v2.run_backtest_panels(calendar, panels, names, req)
    assert any("US1" in r.holdings for r in rebalances if not r.frozen)
    for r in rebalances:
        if r.frozen:
            continue
        sig = date.fromisoformat(r.signal_date)
        fill = date.fromisoformat(r.fill_date)
        assert (fill - sig).days == 2  # 含 QDII → T+2
    assert all(t.settle_lag == 2 for t in trades)


def test_weight_caps_hold_in_backtest() -> None:
    days = 400
    calendar = _make_calendar(days)
    panels = {f"CN{i}": _trend_panel(days, 0.0003 * (i + 1)) for i in range(6)}
    names = {f"CN{i}": f"基金{i}号行业精选股票" for i in range(6)}
    req = BacktestV2Request(candidate_codes=list(panels), top_n=6)
    _, _, rebalances, _, _ = v2.run_backtest_panels(calendar, panels, names, req)
    for r in rebalances:
        assert all(w <= 0.08 + 1e-9 for w in r.holdings.values())  # 单基金 8%
        assert sum(r.holdings.values()) + r.cash_weight <= 1.0 + 1e-9  # 不卖空
        assert all(w > 0 for w in r.holdings.values())


# ---------------------------------------------------------------------------
# 冻结（高波动 + 急反弹）
# ---------------------------------------------------------------------------


def _high_vol_rebound_panel(
    days: int, vol_from: int, rebound_from: int,
    noise: float = 0.030, rebound: float = 0.020, drift: float = 0.002,
) -> list[float]:
    """高波动 + 急反弹探针：vol_from 起持续 ±noise 高波，rebound_from 起连续反弹。

    组合近满仓时（12+ 只入选各 8%）EWMA60 年化波动可超过 25% 阈值。
    """
    values = [1.0]
    for i in range(days - 1):
        idx = i + 1
        if idx >= rebound_from:
            values.append(values[-1] * (1 + rebound))
        elif idx >= vol_from:
            jitter = noise if idx % 2 == 0 else -noise
            values.append(values[-1] * (1 + drift + jitter))
        else:
            values.append(values[-1] * (1 + 0.001))
    return values


def test_freeze_keeps_previous_holdings() -> None:
    """组合 EWMA60 ≥25% 且急反弹时，本期冻结：沿用持仓、换手为零、无成交。

    探针：14 只候选在 2025-01 月末信号前 55 日起进入持续 ±3% 高波，
    信号前 5 日起连续 +2% 急反弹；组合近满仓（14 只各 8% 截断），
    EWMA60 年化波动超过 25%，近 5 日组合收益 ≥8%，触发冻结。
    """
    days = 420
    calendar = _make_calendar(days)
    jan_signal = max(i for i, d in enumerate(calendar) if d <= date(2025, 1, 30))
    panels = {
        f"CN{i:02d}": _high_vol_rebound_panel(
            days, jan_signal - 55, jan_signal - 5,
            noise=0.030 - i * 0.0002, rebound=0.020 - i * 0.0001,
        )
        for i in range(14)
    }
    names = {f"CN{i:02d}": f"精选{i}号行业股票" for i in range(14)}
    # 放开权重约束与波动率目标（caps=1、目标波动 50%），使组合近满仓持有
    # 高波资产，直接检验 高波动 + 急反弹 冻结规则本身
    req = BacktestV2Request(
        candidate_codes=list(panels), top_n=14,
        max_fund_weight=1.0, max_family_weight=1.0, max_qdii_weight=1.0,
        target_vol=0.50,
    )
    _, _, rebalances, trades, _ = v2.run_backtest_panels(calendar, panels, names, req)

    frozen = [r for r in rebalances if r.frozen]
    assert frozen, "高波动+急反弹应触发至少一次冻结"
    for r in frozen:
        assert r.turnover == 0.0
        assert r.realized_vol is not None and r.realized_vol >= risk.FREEZE_HIGH_VOL
        assert "冻结" in r.reason or "急反弹" in r.reason
    # 冻结期不产生该信号日的成交
    frozen_fills = {r.fill_date for r in frozen}
    assert all(t.fill_date not in frozen_fills for t in trades)


# ---------------------------------------------------------------------------
# 波动率目标（只降仓）
# ---------------------------------------------------------------------------


def test_vol_target_only_reduces() -> None:
    """高波组合：波动率目标系数 <1，总仓位下降、现金上升；且只降仓（系数 ≤1）。"""
    days = 400
    calendar = _make_calendar(days)
    # 14 只持续 ±3% 高波 → 满仓组合年化波动 ≈ 47%，远超 11% 带宽
    panels = {
        f"VOL{i:02d}": _high_vol_rebound_panel(
            days, vol_from=0, rebound_from=days + 1, noise=0.030 - i * 0.0002,
        )
        for i in range(14)
    }
    names = {f"VOL{i:02d}": f"精选{i}号行业股票" for i in range(14)}
    req = BacktestV2Request(candidate_codes=list(panels), top_n=14)
    _, _, rebalances, _, _ = v2.run_backtest_panels(calendar, panels, names, req)
    reduced = [r for r in rebalances if not r.frozen and r.vol_scalar < 1.0]
    assert reduced, "高波组合应触发波动率目标降仓"
    for r in reduced:
        assert r.cash_weight > 0
        assert sum(r.holdings.values()) <= r.vol_scalar + 1e-6
    assert all(r.vol_scalar <= 1.0 for r in rebalances)  # 只降仓


# ---------------------------------------------------------------------------
# 集成：POST /api/quant/v2/backtest
# ---------------------------------------------------------------------------


def test_backtest_endpoint_happy_path(client: TestClient, db_session: Session) -> None:
    instruments = [
        _seed_navs(db_session, f"1100{i:02d}", f"精选{i}号行业股票", days=420,
                   daily_growth=0.0012 - i * 0.0003)
        for i in range(4)
    ]
    codes = [inst.code for inst in instruments]

    response = client.post(
        "/api/quant/v2/backtest",
        json={"candidate_codes": codes, "top_n": 3},
    )
    assert response.status_code == 200
    data = response.json()

    assert data["rebalance_count"] >= 3  # 420 - 253 ≈ 167 天 → ≥7 个信号
    assert data["start_date"] < data["end_date"]
    assert data["strategy"]["total_return"] is not None
    assert data["benchmark"]["total_return"] is not None
    assert data["excess_return"] is not None
    assert len(data["curve"]) > 0
    assert data["methodology"]
    assert data["params"]["fee_model"]["buy_fee_rate"] == 0.0  # 默认零费用
    assert data["total_fees"] == 0.0

    for r in data["rebalances"]:
        assert all(w <= 0.08 + 1e-9 for w in r["holdings"].values())
        assert r["allocation_method"] in ("hrp", "inverse_vol", "equal_weight", "frozen")
    for t in data["trades"]:
        assert t["settle_lag"] in (1, 2)
        assert t["fee"] == 0.0
        sig = date.fromisoformat(t["signal_date"])
        fill = date.fromisoformat(t["fill_date"])
        assert (fill - sig).days == t["settle_lag"]


def test_backtest_endpoint_respects_start_date(client: TestClient, db_session: Session) -> None:
    instruments = [
        _seed_navs(db_session, f"1100{i:02d}", f"精选{i}号行业股票", days=500)
        for i in range(3)
    ]
    codes = [inst.code for inst in instruments]
    response = client.post(
        "/api/quant/v2/backtest",
        json={"candidate_codes": codes, "start_date": "2024-03-01"},
    )
    assert response.status_code == 200
    data = response.json()
    # start_date 之后的区间：2024-03-01（第 60 天）起 440 天，足够 253 动量 + 调仓
    assert data["rebalances"]
    assert all(r["signal_date"] >= "2024-03-01" for r in data["rebalances"])


def test_backtest_endpoint_fee_model_reduces_value(client: TestClient, db_session: Session) -> None:
    instruments = [
        _seed_navs(db_session, f"1100{i:02d}", f"精选{i}号行业股票", days=400)
        for i in range(3)
    ]
    codes = [inst.code for inst in instruments]

    free = client.post("/api/quant/v2/backtest", json={"candidate_codes": codes}).json()
    charged = client.post(
        "/api/quant/v2/backtest",
        json={
            "candidate_codes": codes,
            # 组合净值按 1 元起点归一，min_fee=0 保证小额成交也产生费用
            "fee_model": {"buy_fee_rate": 0.01, "sell_fee_rate": 0.005, "slippage_rate": 0.001},
        },
    ).json()

    assert charged["total_fees"] > 0
    assert all(t["fee"] >= 0 for t in charged["trades"])
    assert any(t["fee"] > 0 for t in charged["trades"])
    # 费用侵蚀净值：同区间最终净值低于零费用基线
    assert charged["curve"][-1]["strategy"] < free["curve"][-1]["strategy"]


def test_backtest_endpoint_insufficient_candidates(client: TestClient, db_session: Session) -> None:
    _seed_navs(db_session, "110001", "精选1号行业股票", days=420)
    response = client.post("/api/quant/v2/backtest", json={"candidate_codes": ["110001"]})
    assert response.status_code == 400
    assert "不足" in response.json()["detail"]


def test_backtest_endpoint_insufficient_samples(client: TestClient, db_session: Session) -> None:
    instruments = [
        _seed_navs(db_session, f"1100{i:02d}", f"精选{i}号行业股票", days=100)
        for i in range(3)
    ]
    codes = [inst.code for inst in instruments]
    response = client.post("/api/quant/v2/backtest", json={"candidate_codes": codes})
    assert response.status_code == 400
    assert "不足" in response.json()["detail"]


# ---------------------------------------------------------------------------
# 集成：GET /api/quant/v2/signals
# ---------------------------------------------------------------------------


def test_signals_endpoint_happy_path(client: TestClient, db_session: Session) -> None:
    instruments = [
        _seed_navs(db_session, f"1100{i:02d}", f"精选{i}号行业股票", days=400,
                   daily_growth=0.0012 - i * 0.0002)
        for i in range(5)
    ]
    # 一只 QDII（动量最高）与一只负动量基金
    _seed_navs(db_session, "513100", "国泰纳斯达克100指数QDII", days=400, daily_growth=0.0018)
    _seed_navs(db_session, "110099", "衰退行业主题股票", days=400, daily_growth=-0.001)
    codes = [inst.code for inst in instruments] + ["513100", "110099"]

    response = client.get(
        "/api/quant/v2/signals", params={"codes": ",".join(codes), "top_n": 4}
    )
    assert response.status_code == 200
    data = response.json()

    assert data["as_of"]
    assert data["candidate_count"] == 7
    assert data["selected"], "应有入选基金"
    selected_codes = {item["code"] for item in data["selected"]}
    assert "110099" not in selected_codes  # 负动量被过滤
    assert "513100" in selected_codes  # QDII 动量最高应入选

    for item in data["selected"]:
        assert item["momentum_12_1"] > 0
        assert 0 < item["weight"] <= 0.08 + 1e-9
        assert item["rank_in_market"] >= 1
        assert item["reasons"]

    total_weight = sum(item["weight"] for item in data["selected"])
    assert total_weight + data["cash_weight"] <= 1.0 + 1e-9
    assert 0 < data["vol_scalar"] <= 1.0
    assert data["frozen"] is False
    # QDII 入选 → 预计成交日为基准日 T+2
    assert data["trade_date"] is not None
    lag = (date.fromisoformat(data["trade_date"]) - date.fromisoformat(data["as_of"])).days
    assert lag == 2  # QDII → T+2


def test_signals_endpoint_defaults_to_positions(client: TestClient, db_session: Session) -> None:
    instruments = [
        _seed_navs(db_session, f"1100{i:02d}", f"精选{i}号行业股票", days=400)
        for i in range(3)
    ]
    for inst in instruments:
        _seed_position(db_session, inst)

    response = client.get("/api/quant/v2/signals")
    assert response.status_code == 200
    data = response.json()
    assert data["candidate_count"] == 3


def test_signals_endpoint_weight_caps(client: TestClient, db_session: Session) -> None:
    """大量同层候选时：单基金 ≤8%、总权重 ≤1。"""
    instruments = [
        _seed_navs(db_session, f"110{i:03d}", f"精选{i}号行业股票", days=400,
                   daily_growth=0.0005 + i * 0.0001)
        for i in range(10)
    ]
    codes = [inst.code for inst in instruments]
    response = client.get("/api/quant/v2/signals", params={"codes": ",".join(codes), "top_n": 10})
    assert response.status_code == 200
    data = response.json()
    for item in data["selected"]:
        assert item["weight"] <= 0.08 + 1e-9
    assert sum(item["weight"] for item in data["selected"]) <= 1.0 + 1e-9


def test_signals_endpoint_no_positions_400(client: TestClient) -> None:
    response = client.get("/api/quant/v2/signals")
    assert response.status_code == 400


# ---------------------------------------------------------------------------
# 基准锚定与费用下现金非负
# ---------------------------------------------------------------------------


def test_benchmark_anchored_at_first_fill_day() -> None:
    """基准（B0）首个成交日锚定 1.0，且与策略曲线日期、长度逐一对齐。"""
    days = 380
    calendar = _make_calendar(days)
    panels = _base_panels(days)
    req = BacktestV2Request(candidate_codes=list(panels), top_n=3)
    strategy, benchmark, rebalances, _, _ = v2.run_backtest_panels(
        calendar, panels, _BASE_NAMES, req
    )
    assert len(strategy) == len(benchmark)
    # 首个成交日基准净值 = 1.0（修复前锚定前一交易日，首点 ≠ 1.0）
    assert benchmark[0] == pytest.approx(1.0)
    # 策略曲线同样从 1.0 起步（起点 1 元归一）
    assert strategy[0] == pytest.approx(1.0)
    # 曲线日历与净值序列等长（_sample_curve 按同一 calendar 切片对齐）
    first_fill = len(calendar) - len(strategy)
    first_rebalance = rebalances[0]
    assert first_rebalance.fill_date == calendar[first_fill].isoformat()


def test_backtest_cash_never_negative_with_fees() -> None:
    """非零费用模型下，调仓扣费后现金不为负（不允许融资），净值恒非负。"""
    days = 380
    calendar = _make_calendar(days)
    panels = _base_panels(days)
    # 极端费率（买/卖各 10% 上限）放大费用冲击，强制检验现金下界
    req = BacktestV2Request(
        candidate_codes=list(panels),
        top_n=3,
        fee_model=FeeModelConfig(
            buy_fee_rate=0.10, sell_fee_rate=0.10, slippage_rate=0.0, min_fee=0.0
        ),
    )
    strategy, _, rebalances, trades, _ = v2.run_backtest_panels(
        calendar, panels, _BASE_NAMES, req
    )
    assert any(t.fee > 0 for t in trades), "应产生非零费用"
    assert all(value >= 0 for value in strategy)
    for r in rebalances:
        assert 0.0 <= r.cash_weight <= 1.0 + 1e-9


def test_methodology_declares_survivorship_bias() -> None:
    """方法论声明当前候选池的幸存者偏差。"""
    assert "幸存者偏差" in v2.METHODOLOGY_V2
    assert "当前候选池" in v2.METHODOLOGY_V2
