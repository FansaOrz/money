"""量化研究模块测试：单基金指标、回测、组合摘要。

使用合成的确定性净值序列，不依赖外部行情。
"""

from datetime import date, timedelta
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Account, FundNav, Instrument, Position


def _seed_instrument_with_navs(
    db: Session,
    code: str = "110022",
    name: str = "测试基金",
    days: int = 300,
    start_nav: float = 1.0,
    daily_growth: float = 0.001,
) -> Instrument:
    """写入一只基金及等比增长的净值序列（确定性的，便于断言）。"""
    instrument = Instrument(code=code, name=name)
    db.add(instrument)
    db.flush()
    base = date(2025, 1, 1)
    nav = start_nav
    for i in range(days):
        db.add(
            FundNav(
                instrument_id=instrument.id,
                nav_date=base + timedelta(days=i),
                unit_nav=Decimal(f"{nav:.6f}"),
                accumulated_nav=Decimal(f"{nav:.6f}"),
                source="test",
            )
        )
        nav *= 1 + daily_growth
    db.commit()
    return instrument


def test_fund_indicators(client: TestClient, db_session: Session) -> None:
    """指标接口返回各周期收益与趋势信号。"""
    _seed_instrument_with_navs(db_session)
    response = client.get("/api/quant/indicators/110022")
    assert response.status_code == 200
    data = response.json()
    assert data["code"] == "110022"
    assert data["sample_count"] == 300
    # 每日 +0.1% 等比增长：20 日收益 ≈ 1.001^20 - 1 ≈ 0.02019
    assert data["return_20d"] == pytest.approx(1.001**20 - 1, rel=1e-4)
    assert data["return_60d"] == pytest.approx(1.001**60 - 1, rel=1e-4)
    assert data["return_250d"] == pytest.approx(1.001**250 - 1, rel=1e-4)
    # 恒定增长：日收益近似恒定（净值按 6 位小数存储，存在微小舍入抖动）
    assert data["annual_volatility"] == pytest.approx(0.0, abs=1e-4)
    assert data["max_drawdown"] == pytest.approx(0.0, abs=1e-6)
    assert data["ma20"] is not None
    assert data["ma60"] is not None
    assert data["ma20"] > data["ma60"]  # 上升趋势中短期均线在长期均线上方
    assert data["macd_dif"] is not None
    assert data["trend_signal"] in ("strong_up", "up")
    assert len(data["trend_reasons"]) > 0


def test_fund_indicators_unknown_code(client: TestClient) -> None:
    """未知基金返回 400。"""
    response = client.get("/api/quant/indicators/999999")
    assert response.status_code == 400
    assert "未找到基金" in response.json()["detail"]


def test_fund_indicators_insufficient_navs(client: TestClient, db_session: Session) -> None:
    """净值样本不足时返回 400。"""
    _seed_instrument_with_navs(db_session, days=1)
    response = client.get("/api/quant/indicators/110022")
    assert response.status_code == 400
    assert "样本不足" in response.json()["detail"]


def test_backtest_buy_hold(client: TestClient, db_session: Session) -> None:
    """买入持有：期末价值 = 初始资金 × 净值涨幅。"""
    _seed_instrument_with_navs(db_session, days=100, daily_growth=0.002)
    response = client.post(
        "/api/quant/backtest",
        json={"code": "110022", "strategy": "buy_hold", "initial_capital": 10000},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["strategy"] == "buy_hold"
    assert data["trade_count"] == 1
    # 总收益 ≈ 1.002^99 - 1
    assert data["total_return"] == pytest.approx(1.002**99 - 1, rel=1e-3)
    assert data["final_value"] == pytest.approx(10000 * 1.002**99, rel=1e-3)
    assert data["max_drawdown"] == pytest.approx(0.0, abs=1e-9)
    assert len(data["curve"]) >= 2
    assert data["curve"][0]["value"] == pytest.approx(10000, rel=1e-3)


def test_backtest_ma_cross(client: TestClient, db_session: Session) -> None:
    """MA 交叉策略在单边上涨行情应产生金叉买入信号。"""
    _seed_instrument_with_navs(db_session, days=120, daily_growth=0.003)
    response = client.post(
        "/api/quant/backtest",
        json={
            "code": "110022",
            "strategy": "ma_cross",
            "fast_window": 5,
            "slow_window": 20,
        },
    )
    assert response.status_code == 200
    data = response.json()
    assert data["trade_count"] >= 1
    assert data["signals"][0]["action"] == "buy"
    assert "金叉" in data["signals"][0]["reason"]
    # 单边上涨全程持有：收益接近买入持有
    assert data["total_return"] is not None and data["total_return"] > 0


def test_backtest_ma_cross_param_validation(client: TestClient, db_session: Session) -> None:
    """fast_window >= slow_window 时返回 400。"""
    _seed_instrument_with_navs(db_session, days=60)
    response = client.post(
        "/api/quant/backtest",
        json={"code": "110022", "strategy": "ma_cross", "fast_window": 60, "slow_window": 20},
    )
    assert response.status_code == 400


def test_backtest_macd(client: TestClient, db_session: Session) -> None:
    """MACD 策略可运行并输出指标。"""
    _seed_instrument_with_navs(db_session, days=120, daily_growth=0.002)
    response = client.post(
        "/api/quant/backtest",
        json={"code": "110022", "strategy": "macd"},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["strategy"] == "macd"
    assert data["total_return"] is not None


def test_backtest_dca(client: TestClient, db_session: Session) -> None:
    """定投策略按期追加投入，本金口径正确。"""
    _seed_instrument_with_navs(db_session, days=100, daily_growth=0.001)
    response = client.post(
        "/api/quant/backtest",
        json={
            "code": "110022",
            "strategy": "dca",
            "initial_capital": 1000,
            "invest_interval": 20,
            "invest_amount": 500,
        },
    )
    assert response.status_code == 200
    data = response.json()
    # 首期 + 第20/40/60/80 日各一期 = 5 次买入
    assert data["trade_count"] == 5
    # 累计本金 = 1000 + 4*500 = 3000，体现在响应的 initial_capital 字段
    assert data["initial_capital"] == pytest.approx(3000)
    assert data["final_value"] > 3000  # 每日正收益，市值应高于累计本金


def test_backtest_grid(client: TestClient, db_session: Session) -> None:
    """网格策略在波动行情中产生买卖信号。"""
    instrument = Instrument(code="320007", name="波动测试基金")
    db_session.add(instrument)
    db_session.flush()
    base = date(2025, 1, 1)
    nav = 1.0
    # 构造锯齿行情：涨跌交替，触发网格买卖
    for i in range(120):
        db_session.add(
            FundNav(
                instrument_id=instrument.id,
                nav_date=base + timedelta(days=i),
                unit_nav=Decimal(f"{nav:.6f}"),
                source="test",
            )
        )
        nav *= 0.97 if i % 8 < 4 else 1.04
    db_session.commit()

    response = client.post(
        "/api/quant/backtest",
        json={"code": "320007", "strategy": "grid", "grid_step": 0.03, "grid_amount": 1000},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["trade_count"] >= 3  # 首建半仓 + 至少一次买一次卖
    actions = {s["action"] for s in data["signals"]}
    assert actions == {"buy", "sell"}


def test_backtest_date_filter(client: TestClient, db_session: Session) -> None:
    """指定回测区间时，仅使用该区间数据。"""
    _seed_instrument_with_navs(db_session, days=300)
    response = client.post(
        "/api/quant/backtest",
        json={
            "code": "110022",
            "strategy": "buy_hold",
            "start_date": "2025-02-01",
            "end_date": "2025-03-01",
        },
    )
    assert response.status_code == 200
    data = response.json()
    assert data["start_date"] >= "2025-02-01"
    assert data["end_date"] <= "2025-03-01"


def test_backtest_invalid_dates(client: TestClient, db_session: Session) -> None:
    """起始日晚于截止日返回 400。"""
    _seed_instrument_with_navs(db_session, days=60)
    response = client.post(
        "/api/quant/backtest",
        json={
            "code": "110022",
            "strategy": "buy_hold",
            "start_date": "2025-06-01",
            "end_date": "2025-01-01",
        },
    )
    assert response.status_code == 400


# ---------------------------------------------------------------------------
# 净值口径与回测执行口径（总收益指数 / 单位净值成交价 / T+1 / TWR / 网格锚点）
# ---------------------------------------------------------------------------


def _seed_dividend_fund(db: Session, code: str = "110066") -> Instrument:
    """构造一只在区间中段发生分红的基金（确定性的，便于手算断言）。

    单位净值：1.00, 1.10, 1.00（除息回落）, 1.10
    累计净值：1.00, 1.10, 1.20, 1.30（累计净值 = 单位净值 + 累计每股分红 0.20）
    总收益日收益：+10%, +9.0909...%, +10%（真实总回报 32%）；
    若按单位净值逐点拼接则次日收益为 -9.09%，口径错误。
    """
    instrument = Instrument(code=code, name="分红测试基金")
    db.add(instrument)
    db.flush()
    base = date(2025, 1, 1)
    navs = [
        ("1.0000", "1.0000"),
        ("1.1000", "1.1000"),
        ("1.0000", "1.2000"),  # 每股分红 0.20 除息，单位净值回落但累计净值连续
        ("1.1000", "1.3000"),
    ]
    for i, (unit, acc) in enumerate(navs):
        db.add(
            FundNav(
                instrument_id=instrument.id,
                nav_date=base + timedelta(days=i),
                unit_nav=Decimal(unit),
                accumulated_nav=Decimal(acc),
                source="test",
            )
        )
    db.commit()
    return instrument


def test_backtest_buy_hold_uses_accumulated_total_return(
    client: TestClient, db_session: Session
) -> None:
    """buy_hold 手算：成交价为单位净值 1.0 → 份额 10000；期末按总收益口径
    估值 = 10000 × 1.32（累计净值连续，分红留存组合），总收益 = 32%。
    若误用单位净值估值/拼接，总收益会被低估为 10% 并出现除息假回撤。"""
    _seed_dividend_fund(db_session)
    response = client.post(
        "/api/quant/backtest",
        json={"code": "110066", "strategy": "buy_hold", "initial_capital": 10000},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["trade_count"] == 1
    signal = data["signals"][0]
    # 成交价为首个单位净值 1.0，份额 10000
    assert signal["price"] == pytest.approx(1.0)
    assert signal["shares"] == pytest.approx(10000.0)
    # 期末市值按总收益（累计净值）口径计价：10000 份 × 1.32
    assert data["final_value"] == pytest.approx(13200.0)
    # 总收益为含分红总回报 32%，而非单位净值口径的 10%
    assert data["total_return"] == pytest.approx(0.32, rel=1e-9)
    # TWR 日收益与累计净值日收益一致，不含单位净值除息跳变
    assert data["max_drawdown"] == pytest.approx(0.0, abs=1e-9)


def test_dual_nav_series_no_unit_mixing(db_session: Session) -> None:
    """累计净值部分缺测时，总收益序列在衔接处连续（收益等于单位净值日收益），
    不发生单位切换跳变；成交价序列始终保持单位净值口径。"""
    from app.services.quant import _load_dual_nav_series

    instrument = Instrument(code="110077", name="缺测累计净值基金")
    db_session.add(instrument)
    db_session.flush()
    base = date(2025, 1, 1)
    # 前两天仅有单位净值，之后补齐累计净值
    rows = [
        ("1.0000", None),
        ("1.1000", None),  # +10%
        ("1.2100", "1.2100"),  # +10%
        ("1.3310", "1.3310"),  # +10%
    ]
    for i, (unit, acc) in enumerate(rows):
        db_session.add(
            FundNav(
                instrument_id=instrument.id,
                nav_date=base + timedelta(days=i),
                unit_nav=Decimal(unit),
                accumulated_nav=Decimal(acc) if acc else None,
                source="test",
            )
        )
    db_session.commit()

    pair = _load_dual_nav_series(db_session, instrument.id)
    totals = pair.total_values
    units = pair.unit_values
    assert pair.unit_fallback is True
    # 成交价序列始终为单位净值
    assert units == pytest.approx([1.0, 1.1, 1.21, 1.331])
    # 总收益序列每日收益恒为 +10%：缺测段按衔接比率缩放，不产生跳变
    for i in range(1, len(totals)):
        assert totals[i] / totals[i - 1] - 1 == pytest.approx(0.10, rel=1e-9)
    # 累计净值存在的点原样保留
    assert totals[-1] == pytest.approx(1.331)
    assert totals[-2] == pytest.approx(1.21)


def test_macd_hist_convention() -> None:
    """MACD 柱口径为 2×(DIF-DEA)（与国内行情软件一致）。"""
    from app.services.quant import _macd_series

    values = [1.0 + 0.01 * i for i in range(60)]
    dif, dea, hist = _macd_series(values)
    assert len(dif) == len(dea) == len(hist) == len(values)
    for d, e, h in zip(dif, dea, hist, strict=True):
        assert h == pytest.approx(2.0 * (d - e), rel=1e-12)


def test_backtest_ma_cross_t_plus_1_execution(client: TestClient, db_session: Session) -> None:
    """MA 信号 T 日生成、T+1 按单位净值成交：金叉判定日为第 5 天（index 4），
    信号日期与成交价都必须落在第 6 天（index 5）。"""
    instrument = Instrument(code="110088", name="T+1测试基金")
    db_session.add(instrument)
    db_session.flush()
    base = date(2025, 1, 1)
    # 前 4 天阴跌（MA3 < MA5），第 5 天（index 4）大幅反弹 25% 至 1.20，
    # 当日收盘 MA3 上穿 MA5（真金叉，起点顺势条件在 index=3 不成立）：
    # index=3: MA3=0.97 < MA5=0.98（不建仓）；index=4: MA3=1.0467 > MA5=1.032
    closes = [1.0, 0.98, 0.97, 0.96, 1.20, 1.50, 1.60]
    for i, close in enumerate(closes):
        db_session.add(
            FundNav(
                instrument_id=instrument.id,
                nav_date=base + timedelta(days=i),
                unit_nav=Decimal(f"{close:.6f}"),
                accumulated_nav=Decimal(f"{close:.6f}"),
                source="test",
            )
        )
    db_session.commit()

    response = client.post(
        "/api/quant/backtest",
        json={
            "code": "110088",
            "strategy": "ma_cross",
            "fast_window": 3,
            "slow_window": 5,
            "initial_capital": 10000,
        },
    )
    assert response.status_code == 200
    data = response.json()
    assert data["trade_count"] == 1
    signal = data["signals"][0]
    # 金叉于 index=4（2025-01-05）收盘确认，T+1（2025-01-06）以当日净值 1.5 成交
    # 若当日成交则买入价为金叉日的 1.20（未来函数）
    assert signal["date"] == "2025-01-06"
    assert signal["price"] == pytest.approx(1.5)
    assert "T+1" in signal["reason"]
    # 手算：10000 / 1.5 份，期末市值 = 份额 × 1.6
    shares = 10000 / 1.5
    assert signal["shares"] == pytest.approx(shares, rel=1e-4)
    assert data["final_value"] == pytest.approx(shares * 1.6, rel=1e-3)
    assert data["total_return"] == pytest.approx(shares * 1.6 / 10000 - 1, rel=1e-3)


def test_backtest_ma_cross_no_lookahead_entry_day(
    client: TestClient, db_session: Session
) -> None:
    """T 日信号必须 T+1 成交：金叉确认日的大涨不得被计入成交价。

    价格：前 5 天平稳（均线空头），index=4 大涨 20% 使金叉于当日收盘确认。
    若当日成交则买入价为 1.20（偷看当日涨幅），正确实现应以次日 1.21 成交。
    """
    instrument = Instrument(code="110089", name="反未来函数基金")
    db_session.add(instrument)
    db_session.flush()
    base = date(2025, 1, 1)
    closes = [1.04, 1.03, 1.02, 1.01, 1.00, 1.20, 1.21, 1.22]
    for i, close in enumerate(closes):
        db_session.add(
            FundNav(
                instrument_id=instrument.id,
                nav_date=base + timedelta(days=i),
                unit_nav=Decimal(f"{close:.6f}"),
                accumulated_nav=Decimal(f"{close:.6f}"),
                source="test",
            )
        )
    db_session.commit()

    response = client.post(
        "/api/quant/backtest",
        json={
            "code": "110089",
            "strategy": "ma_cross",
            "fast_window": 3,
            "slow_window": 5,
            "initial_capital": 10000,
        },
    )
    assert response.status_code == 200
    data = response.json()
    assert data["trade_count"] == 1
    signal = data["signals"][0]
    # index=4 收盘 MA3=(1.02+1.01+1.00)/3=1.01 ≤ MA5=1.02；
    # index=5 收盘 MA3=(1.01+1.00+1.20)/3=1.07 > MA5=1.058 → 金叉确认
    # T+1（index=6）以 1.21 成交，而非金叉日的 1.20
    assert signal["date"] == (base + timedelta(days=6)).isoformat()
    assert signal["price"] == pytest.approx(1.21)


def test_backtest_dca_twr_excludes_cashflow(client: TestClient, db_session: Session) -> None:
    """定投注入资金不产生 TWR 收益：第二期在高点投入后净值回落，
    TWR 年化/回撤只反映净值路径，注入本身不被计为收益。"""
    instrument = Instrument(code="110099", name="TWR测试基金")
    db_session.add(instrument)
    db_session.flush()
    base = date(2025, 1, 1)
    # index 0→1 涨 10%，index 1→2 跌 10%；在 index=1 追加 1000
    closes = [1.0, 1.1, 0.99]
    for i, close in enumerate(closes):
        db_session.add(
            FundNav(
                instrument_id=instrument.id,
                nav_date=base + timedelta(days=i),
                unit_nav=Decimal(f"{close:.6f}"),
                accumulated_nav=Decimal(f"{close:.6f}"),
                source="test",
            )
        )
    db_session.commit()

    response = client.post(
        "/api/quant/backtest",
        json={
            "code": "110099",
            "strategy": "dca",
            "initial_capital": 1000,
            "invest_interval": 1,
            "invest_amount": 1000,
        },
    )
    assert response.status_code == 200
    data = response.json()
    # TWR 日收益为 +10%、-10%：与净值路径一致，与现金流规模无关。
    # 若注入被误判为收益（旧归一化口径），回撤会被摊薄为约 -4.76%。
    # 期末：份额 = 1000/1.0 + 1000/1.1 + 1000/0.99，市值 = 份额 × 0.99
    shares = 1000 / 1.0 + 1000 / 1.1 + 1000 / 0.99
    expected_final = shares * 0.99
    assert data["final_value"] == pytest.approx(expected_final, rel=1e-3)
    assert data["initial_capital"] == pytest.approx(3000)
    assert data["total_return"] == pytest.approx(expected_final / 3000 - 1, rel=1e-3)
    # TWR 财富指数 1.0 → 1.1 → 0.99：最大回撤恰为 -10%（剔除现金流）
    assert data["max_drawdown"] == pytest.approx(-0.10, rel=1e-3)


def test_backtest_grid_anchors_last_real_fill(client: TestClient, db_session: Session) -> None:
    """网格：现金耗尽后触及买格不成交、不移动锚点、不记录信号；
    后续卖出以最近一次真实成交价为基准。"""
    instrument = Instrument(code="110111", name="网格锚点基金")
    db_session.add(instrument)
    db_session.flush()
    base = date(2025, 1, 1)
    # 1.00 → 0.90（-10%，买一格，现金耗尽）→ 0.80（-11%，无现金不成交、锚点不动）
    # → 0.99（相对锚点 0.90 涨 10%，卖出一格）
    closes = [1.0, 0.90, 0.80, 0.99]
    for i, close in enumerate(closes):
        db_session.add(
            FundNav(
                instrument_id=instrument.id,
                nav_date=base + timedelta(days=i),
                unit_nav=Decimal(f"{close:.6f}"),
                accumulated_nav=Decimal(f"{close:.6f}"),
                source="test",
            )
        )
    db_session.commit()

    response = client.post(
        "/api/quant/backtest",
        json={
            "code": "110111",
            "strategy": "grid",
            "initial_capital": 2000,
            "grid_step": 0.10,
            "grid_amount": 1000,
        },
    )
    assert response.status_code == 200
    data = response.json()
    signals = data["signals"]
    # 首建半仓 1000 @1.00（现金剩 1000）；0.90 买一格 1000（现金归零，锚点 0.90）；
    # 0.80 触及买格但无现金：不成交、不记录、锚点保持 0.90；
    # 0.99 相对锚点涨 10%：卖出 1000（持仓 1000/1.0+1000/0.9 份 @0.99 市值充足）
    assert [s["action"] for s in signals] == ["buy", "buy", "sell"]
    assert signals[0]["price"] == pytest.approx(1.00)
    assert signals[1]["price"] == pytest.approx(0.90)
    assert signals[1]["amount"] == pytest.approx(1000.0)
    assert signals[2]["price"] == pytest.approx(0.99)
    assert signals[2]["amount"] == pytest.approx(1000.0)
    assert signals[2]["date"] == (base + timedelta(days=3)).isoformat()
    assert data["trade_count"] == 3  # 0.80 未成交，不产生第 4 条信号
    # 手算期末：份额 = 1000/1.0 + 1000/0.9 - 1000/0.99，现金 = 1000（卖出回款）
    shares = 1000 / 1.0 + 1000 / 0.9 - 1000 / 0.99
    expected_final = 1000 + shares * 0.99
    assert data["final_value"] == pytest.approx(expected_final, rel=1e-3)


def test_backtest_insufficient_samples_explicit_error(
    client: TestClient, db_session: Session
) -> None:
    """样本不足以支撑策略窗口时返回明确错误信息（含所需样本数）。"""
    _seed_instrument_with_navs(db_session, days=10)
    response = client.post(
        "/api/quant/backtest",
        json={"code": "110022", "strategy": "ma_cross", "fast_window": 5, "slow_window": 20},
    )
    assert response.status_code == 400
    detail = response.json()["detail"]
    assert "样本" in detail
    assert "22" in detail  # slow_window(20) + 2

    response = client.post(
        "/api/quant/backtest",
        json={"code": "110022", "strategy": "macd"},
    )
    assert response.status_code == 400
    assert "样本" in response.json()["detail"]


def test_portfolio_metrics_empty(client: TestClient) -> None:
    """空组合返回零值摘要。"""
    response = client.get("/api/quant/portfolio-metrics")
    assert response.status_code == 200
    data = response.json()
    assert data["total_market_value"] == "0"
    assert data["position_count"] == 0
    assert data["holdings"] == []
    assert data["signals"] == []


def test_portfolio_metrics_concentration(client: TestClient, db_session: Session) -> None:
    """高度集中的组合触发集中度风险信号。"""
    instrument = _seed_instrument_with_navs(db_session, days=300)
    account = Account(name="测试账户")
    db_session.add(account)
    db_session.flush()
    db_session.add(
        Position(
            account_id=account.id,
            instrument_id=instrument.id,
            shares=Decimal("10000"),
            cost=Decimal("10000.00"),
            market_value=Decimal("12000.00"),
        )
    )
    db_session.commit()

    response = client.get("/api/quant/portfolio-metrics")
    assert response.status_code == 200
    data = response.json()
    assert data["position_count"] == 1
    assert data["concentration_top1"] == pytest.approx(1.0)
    assert data["holdings"][0]["weight"] == pytest.approx(1.0)
    categories = {s["category"] for s in data["signals"]}
    assert "concentration" in categories
    levels = {s["level"] for s in data["signals"]}
    assert "risk" in levels
    # 单基金上升趋势，应有 trend_signal
    assert data["holdings"][0]["trend_signal"] in ("strong_up", "up")


def test_portfolio_metrics_diversified(client: TestClient, db_session: Session) -> None:
    """分散持仓的组合 HHI 较低，不触发高集中告警。"""
    account = Account(name="测试账户")
    db_session.add(account)
    db_session.flush()
    for i in range(4):
        instrument = _seed_instrument_with_navs(
            db_session, code=f"00000{i + 1}", name=f"基金{i}", days=60
        )
        db_session.add(
            Position(
                account_id=account.id,
                instrument_id=instrument.id,
                shares=Decimal("1000"),
                cost=Decimal("1000.00"),
                market_value=Decimal("1000.00"),
            )
        )
    db_session.commit()

    response = client.get("/api/quant/portfolio-metrics")
    assert response.status_code == 200
    data = response.json()
    assert data["position_count"] == 4
    assert data["concentration_top1"] == pytest.approx(0.25)
    assert data["hhi"] == pytest.approx(0.25, abs=1e-9)
    # 无集中度 risk 级别信号
    risk_signals = [s for s in data["signals"] if s["level"] == "risk"]
    assert risk_signals == []


def test_fund_indicators_win_rate(client: TestClient, db_session: Session) -> None:
    """恒定正增长基金胜率应为 100%（净值 6 位小数存储，存在微小舍入）。"""
    _seed_instrument_with_navs(db_session)
    response = client.get("/api/quant/indicators/110022")
    assert response.status_code == 200
    data = response.json()
    assert data["win_rate"] == pytest.approx(1.0, abs=0.02)
    assert data["data_available"] is True


def test_list_fund_indicators_merges_market_value(
    client: TestClient, db_session: Session
) -> None:
    """list_fund_indicators 合并持仓市值，无净值持仓以 data_available=false 保留。"""
    account = Account(name="测试账户")
    db_session.add(account)
    db_session.flush()
    # 有净值的持仓
    with_nav = _seed_instrument_with_navs(db_session, code="110022", name="有净值基金", days=120)
    db_session.add(
        Position(
            account_id=account.id,
            instrument_id=with_nav.id,
            shares=Decimal("1000"),
            cost=Decimal("1000.00"),
            market_value=Decimal("1500.00"),
        )
    )
    # 无净值的持仓
    no_nav = Instrument(code="999001", name="无净值基金")
    db_session.add(no_nav)
    db_session.flush()
    db_session.add(
        Position(
            account_id=account.id,
            instrument_id=no_nav.id,
            shares=Decimal("500"),
            cost=Decimal("600.00"),
            market_value=Decimal("800.00"),
        )
    )
    db_session.commit()

    response = client.get("/api/quant/funds")
    assert response.status_code == 200
    data = {item["code"]: item for item in response.json()}
    assert set(data) == {"110022", "999001"}  # 无净值持仓不被丢弃

    assert data["110022"]["data_available"] is True
    assert data["110022"]["market_value"] == "1500.00"
    assert data["110022"]["win_rate"] is not None

    assert data["999001"]["data_available"] is False
    assert data["999001"]["market_value"] == "800.00"
    assert data["999001"]["sample_count"] == 0
    assert data["999001"]["return_20d"] is None
    assert data["999001"]["win_rate"] is None


def test_portfolio_metrics_backtested_portfolio(client: TestClient, db_session: Session) -> None:
    """当前权重回溯组合：两只等权基金日收益 +0.1%，组合指标与单基金一致。"""
    account = Account(name="测试账户")
    db_session.add(account)
    db_session.flush()
    for code, name in (("110022", "基金A"), ("220033", "基金B")):
        instrument = _seed_instrument_with_navs(db_session, code=code, name=name, days=300)
        db_session.add(
            Position(
                account_id=account.id,
                instrument_id=instrument.id,
                shares=Decimal("1000"),
                cost=Decimal("1000.00"),
                market_value=Decimal("1000.00"),
            )
        )
    db_session.commit()

    response = client.get("/api/quant/portfolio-metrics")
    assert response.status_code == 200
    data = response.json()

    assert data["methodology"]
    assert "当前权重" in data["methodology"]
    # 净值为日历日序列（含周末），最后一条净值为 2025-01-01 + 299 天
    assert data["as_of"] == "2025-10-27"
    # 每日收益恒为 +0.1%：区间收益 = 1.001^N - 1，N 为窗口内日收益天数
    assert data["total_return_rate"] == pytest.approx(1.001**299 - 1, rel=1e-3)
    assert data["annualized_return"] == pytest.approx(1.001**252 - 1, rel=1e-3)
    assert data["annualized_volatility"] == pytest.approx(0.0, abs=1e-4)
    assert data["max_drawdown"] == pytest.approx(0.0, abs=1e-6)
    assert data["sharpe_ratio"] is not None and data["sharpe_ratio"] > 0
    assert data["win_rate"] == pytest.approx(1.0, abs=0.02)
    # 原有集中度字段保持
    assert data["concentration_top1"] == pytest.approx(0.5)
    assert data["hhi"] == pytest.approx(0.5, abs=1e-9)


def test_portfolio_metrics_alignment_forward_fill(client: TestClient, db_session: Session) -> None:
    """缺测日期按前值对齐：稀疏净值基金与每日净值基金按共同日期对齐组合收益。"""
    account = Account(name="测试账户")
    db_session.add(account)
    db_session.flush()
    # 基金A：每日净值，日涨 0.1%
    fund_a = _seed_instrument_with_navs(
        db_session, code="110022", name="每日净值基金", days=300, daily_growth=0.001
    )
    # 基金B：仅每 10 天有净值（日期与基金A完全重叠），日涨 0.2%
    fund_b = Instrument(code="220044", name="稀疏净值基金")
    db_session.add(fund_b)
    db_session.flush()
    base = date(2025, 1, 1)
    nav = 1.0
    for i in range(0, 300, 10):
        db_session.add(
            FundNav(
                instrument_id=fund_b.id,
                nav_date=base + timedelta(days=i),
                unit_nav=Decimal(f"{nav:.6f}"),
                accumulated_nav=Decimal(f"{nav:.6f}"),
                source="test",
            )
        )
        nav *= 1.002**10
    for instrument in (fund_a, fund_b):
        db_session.add(
            Position(
                account_id=account.id,
                instrument_id=instrument.id,
                shares=Decimal("1000"),
                cost=Decimal("1000.00"),
                market_value=Decimal("1000.00"),
            )
        )
    db_session.commit()

    response = client.get("/api/quant/portfolio-metrics")
    assert response.status_code == 200
    data = response.json()
    # 等权组合：基金B 有收益的日期（29 天）组合日收益 = (0.001 + (1.002^10 - 1)) / 2，
    # 其余 271 天 = (0.001 + 0) / 2 = 0.0005（前值填充，缺测日记零收益）。
    days_with_b = 29
    days_only_a = 299 - days_with_b
    r_mixed = (0.001 + (1.002**10 - 1)) / 2
    expected_total = (1 + 0.0005) ** days_only_a * (1 + r_mixed) ** days_with_b - 1
    assert data["total_return_rate"] == pytest.approx(expected_total, rel=1e-3)
    assert data["as_of"] == "2025-10-27"
    assert data["win_rate"] == pytest.approx(1.0, abs=0.02)


def test_portfolio_metrics_no_nav_positions(client: TestClient, db_session: Session) -> None:
    """全部持仓均无净值时，回溯指标为 None 但摘要仍正常返回。"""
    account = Account(name="测试账户")
    db_session.add(account)
    db_session.flush()
    instrument = Instrument(code="999002", name="纯持仓基金")
    db_session.add(instrument)
    db_session.flush()
    db_session.add(
        Position(
            account_id=account.id,
            instrument_id=instrument.id,
            shares=Decimal("100"),
            cost=Decimal("100.00"),
            market_value=Decimal("100.00"),
        )
    )
    db_session.commit()

    response = client.get("/api/quant/portfolio-metrics")
    assert response.status_code == 200
    data = response.json()
    assert data["position_count"] == 1
    assert data["as_of"] is None
    assert data["total_return_rate"] is None
    assert data["annualized_return"] is None
    assert data["annualized_volatility"] is None
    assert data["max_drawdown"] is None
    assert data["sharpe_ratio"] is None
    assert data["win_rate"] is None
    assert data["concentration_top1"] == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# 综合研究信号 /api/quant/signals
# ---------------------------------------------------------------------------

from datetime import datetime  # noqa: E402

from app.models import (  # noqa: E402
    FundHolding,
    FundIndustryAllocation,
    FundNewsImpact,
    IndexQuote,
    MarketIndex,
    NewsEvent,
)
from app.services.quant import comprehensive_research_signals  # noqa: E402
from app.schemas.quant import SignalFilters  # noqa: E402


def _seed_position(db: Session, instrument: Instrument, market_value: str = "10000.00") -> None:
    account = db.scalar(select(Account).where(Account.name == "信号测试账户"))
    if account is None:
        account = Account(name="信号测试账户")
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


def test_signals_empty_portfolio(client: TestClient) -> None:
    """空组合：返回空列表与分页元信息，不报 500。"""
    response = client.get("/api/quant/signals")
    assert response.status_code == 200
    data = response.json()
    assert data["total"] == 0
    assert data["signals"] == []
    assert data["limit"] == 100
    assert data["offset"] == 0
    assert "as_of" in data


def test_signals_trend_and_rich_fields(client: TestClient, db_session: Session) -> None:
    """上升趋势持仓产生 trend 信号，且携带 evidence/related_codes/as_of/source。"""
    instrument = _seed_instrument_with_navs(db_session, days=120, daily_growth=0.003)
    _seed_position(db_session, instrument, "10000.00")

    response = client.get("/api/quant/signals")
    assert response.status_code == 200
    data = response.json()
    assert data["total"] >= 1
    trend_signals = [s for s in data["signals"] if s["category"] == "trend"]
    assert trend_signals, "上升趋势应产生 trend 信号"
    signal = trend_signals[0]
    assert signal["related_codes"] == ["110022"]
    assert signal["scope"] == "fund"
    assert signal["source"] == "fund_nav"
    assert signal["as_of"]
    evidence = signal["evidence"]
    assert evidence["trend_signal"] in ("strong_up", "up")
    assert evidence["portfolio_weight"] == pytest.approx(1.0)


def test_signals_category_and_level_filter(client: TestClient, db_session: Session) -> None:
    """category/level 过滤生效，limit/offset 分页生效。"""
    instrument = _seed_instrument_with_navs(db_session, days=120, daily_growth=0.002)
    _seed_position(db_session, instrument, "10000.00")

    # 单基金 100% 权重：必有 concentration risk 信号
    response = client.get("/api/quant/signals", params={"category": "concentration"})
    assert response.status_code == 200
    data = response.json()
    assert data["total"] >= 1
    assert all(s["category"] == "concentration" for s in data["signals"])

    response = client.get("/api/quant/signals", params={"level": "risk"})
    assert response.status_code == 200
    assert all(s["level"] == "risk" for s in response.json()["signals"])

    # 非法枚举值返回 422
    response = client.get("/api/quant/signals", params={"category": "bogus"})
    assert response.status_code == 422

    # 分页：limit=1 时返回 1 条但 total 不变
    full = client.get("/api/quant/signals").json()
    paged = client.get("/api/quant/signals", params={"limit": 1, "offset": 0}).json()
    assert paged["total"] == full["total"]
    assert len(paged["signals"]) == min(1, full["total"])
    if full["total"] > 1:
        page2 = client.get("/api/quant/signals", params={"limit": 1, "offset": 1}).json()
        assert page2["signals"][0] != paged["signals"][0]


def test_signals_stock_exposure_and_overlap(client: TestClient, db_session: Session) -> None:
    """FundHolding 穿透：单股票暴露 ≥5% 与重复持有分别产生信号。"""
    inst_a = _seed_instrument_with_navs(db_session, code="110022", name="基金A", days=60)
    inst_b = _seed_instrument_with_navs(db_session, code="220011", name="基金B", days=60)
    _seed_position(db_session, inst_a, "10000.00")
    _seed_position(db_session, inst_b, "10000.00")

    report_date = date(2025, 3, 31)
    # FundHolding.weight 为百分数口径（20.0 = 20%）。
    # 两只基金各 50% 权重、各以 20% 重仓同一股票：
    # 组合穿透暴露 = 0.5*20% + 0.5*20% = 20%
    for instrument in (inst_a, inst_b):
        db_session.add(
            FundHolding(
                instrument_id=instrument.id,
                report_date=report_date,
                rank=1,
                stock_code="600519",
                stock_name="贵州茅台",
                weight=Decimal("20.00"),
            )
        )
    db_session.commit()

    data = client.get("/api/quant/signals").json()
    categories = {s["category"] for s in data["signals"]}
    assert "stock_exposure" in categories
    assert "overlap" in categories

    exposure = next(s for s in data["signals"] if s["category"] == "stock_exposure")
    assert "600519" in exposure["related_codes"]
    assert set(("110022", "220011")) <= set(exposure["related_codes"])
    assert exposure["evidence"]["look_through_exposure"] == pytest.approx(0.20, abs=1e-6)
    assert exposure["source"] == "fund_holdings"
    assert exposure["level"] == "risk"  # ≥10% 为 risk

    overlap = next(s for s in data["signals"] if s["category"] == "overlap")
    assert overlap["evidence"]["fund_count"] == 2


def test_signals_industry_exposure(client: TestClient, db_session: Session) -> None:
    """行业穿透暴露 ≥20% 产生 industry 信号。"""
    instrument = _seed_instrument_with_navs(db_session, days=60)
    _seed_position(db_session, instrument, "10000.00")
    db_session.add(
        FundIndustryAllocation(
            instrument_id=instrument.id,
            report_date=date(2025, 3, 31),
            industry="白酒",
            weight=Decimal("30.00"),
        )
    )
    db_session.commit()

    data = client.get("/api/quant/signals").json()
    industry = [s for s in data["signals"] if s["category"] == "industry"]
    assert industry, "30% 行业暴露应产生 industry 信号"
    assert industry[0]["evidence"]["industry"] == "白酒"
    assert industry[0]["evidence"]["look_through_exposure"] == pytest.approx(0.30, abs=1e-6)
    assert industry[0]["source"] == "fund_industry_allocations"


def test_signals_news_events(client: TestClient, db_session: Session) -> None:
    """近 7 天存在已去重、已分析的基金事件时产生 news 信号。"""
    instrument = _seed_instrument_with_navs(db_session, days=60)
    _seed_position(db_session, instrument, "10000.00")
    now = datetime.now()
    for i in range(2):
        event = NewsEvent(
            canonical_key=f"event-{i}",
            title=f"已分析基金事件{i}",
            latest_published_at=now,
            direction="positive",
            impact_level="medium",
            impact_score=45,
            horizon_days=7,
            confidence=0.6,
            source_quality=0.8,
            plain_summary="测试事件",
            expires_at=now + timedelta(days=7),
        )
        db_session.add(event)
        db_session.flush()
        db_session.add(
            FundNewsImpact(
                event_id=event.id,
                instrument_id=instrument.id,
                relation_type="direct_fund",
                relevance_score=1,
                exposure_ratio=1,
                signed_score=10,
                reason="新闻直接提到该基金",
            )
        )
    db_session.commit()

    data = client.get("/api/quant/signals").json()
    news = [s for s in data["signals"] if s["category"] == "news"]
    assert news, "已分析且映射到基金的近期事件应产生 news 信号"
    assert news[0]["related_codes"] == ["110022"]
    assert news[0]["evidence"]["event_count"] == 2
    assert news[0]["evidence"]["signed_impact_score"] == pytest.approx(20)
    assert news[0]["source"] == "fund_news_impacts"


def test_signals_market_index(client: TestClient, db_session: Session) -> None:
    """MarketIndex/IndexQuote 存在且指数大跌时产生 market 信号。"""
    instrument = _seed_instrument_with_navs(db_session, days=60)
    _seed_position(db_session, instrument, "10000.00")

    index = MarketIndex(
        code="SH000001", name="上证指数", market="cn", source_symbol="sh000001"
    )
    db_session.add(index)
    db_session.flush()
    base = date(2025, 1, 1)
    price = 3000.0
    for i in range(80):
        db_session.add(
            IndexQuote(
                index_id=index.id,
                trade_date=base + timedelta(days=i),
                close=Decimal(f"{price:.4f}"),
            )
        )
        price *= 0.995  # 持续阴跌，20日收益约 -9.5%
    db_session.commit()

    data = client.get("/api/quant/signals").json()
    market = [s for s in data["signals"] if s["category"] == "market"]
    assert market, "指数近20日下跌 ≥5% 应产生 market 信号"
    signal = market[0]
    assert signal["scope"] == "market"
    assert signal["source"] == "index_quotes"
    assert signal["evidence"]["index_name"] == "上证指数"
    assert signal["evidence"]["return_20d"] < -0.05
    assert "SH000001" in signal["related_codes"]


def test_signals_service_directly(db_session: Session) -> None:
    """直接调用 comprehensive_research_signals：过滤与分页语义正确。"""
    instrument = _seed_instrument_with_navs(db_session, days=120, daily_growth=0.002)
    _seed_position(db_session, instrument, "10000.00")

    result = comprehensive_research_signals(db_session)
    assert result.total == len(result.signals)  # 默认 limit 足够大时全量返回

    risk_only = comprehensive_research_signals(db_session, SignalFilters(level="risk"))
    assert all(s.level == "risk" for s in risk_only.signals)

    none_result = comprehensive_research_signals(
        db_session, SignalFilters(category="news")
    )
    assert none_result.total == 0
    assert none_result.signals == []


# ---------------------------------------------------------------------------
# 净值/指数装载：先按 start/end 过滤、DESC LIMIT 取最新样本后反转为升序
# ---------------------------------------------------------------------------


def test_dual_nav_series_limit_keeps_latest_samples(db_session: Session) -> None:
    """limit 截断保留区间内最新样本（窗口语义），而非最早 limit 条。"""
    from app.services.quant import _load_dual_nav_series

    instrument = _seed_instrument_with_navs(db_session, days=600)
    base = date(2025, 1, 1)

    pair = _load_dual_nav_series(db_session, instrument.id, limit=400)
    assert len(pair.total_series) == 400
    # 起点是第 200 天（600 - 400），而非第 0 天：取的是最新 400 条
    assert pair.total_series[0][0] == base + timedelta(days=200)
    assert pair.total_series[-1][0] == base + timedelta(days=599)
    # 反转为升序
    dates = [d for d, _ in pair.total_series]
    assert dates == sorted(dates)


def test_dual_nav_series_filters_before_limit(db_session: Session) -> None:
    """start/end 先于 limit 生效：区间过滤 + DESC LIMIT + 升序。"""
    from app.services.quant import _load_dual_nav_series

    instrument = _seed_instrument_with_navs(db_session, days=600)
    start = date(2025, 3, 1)
    end = date(2025, 12, 31)

    pair = _load_dual_nav_series(db_session, instrument.id, start=start, end=end, limit=50)
    assert len(pair.total_series) == 50
    # 末尾紧贴 end（区间内最新），起点 ≥ start
    assert pair.total_series[-1][0] == end
    assert pair.total_series[0][0] >= start
    assert pair.total_series[0][0] == end - timedelta(days=49)
    dates = [d for d, _ in pair.total_series]
    assert dates == sorted(dates)


def test_index_quote_series_limit_keeps_latest(db_session: Session) -> None:
    """指数行情装载：DESC LIMIT 取最新后反转为升序，start/end 先行过滤。"""
    from app.services.quant import _index_quote_series, _load_index_models

    index = MarketIndex(code="SH000001", name="上证指数", market="cn", source_symbol="sh")
    db_session.add(index)
    db_session.flush()
    base = date(2024, 1, 1)
    price = 3000.0
    for i in range(600):
        db_session.add(
            IndexQuote(
                index_id=index.id,
                trade_date=base + timedelta(days=i),
                close=Decimal(f"{price:.4f}"),
            )
        )
        price *= 1.001
    db_session.commit()

    index_model, quote_model = _load_index_models()
    assert index_model is not None and quote_model is not None

    series, last_day = _index_quote_series(db_session, index_model, quote_model, index.id, limit=400)
    assert len(series) == 400
    # 最新 400 条：起点为第 200 天而非第 0 天
    assert series[0][0] == base + timedelta(days=200)
    assert last_day == base + timedelta(days=599)
    assert [d for d, _ in series] == sorted(d for d, _ in series)

    bounded, bounded_last = _index_quote_series(
        db_session, index_model, quote_model, index.id,
        start=date(2024, 3, 1), end=date(2024, 12, 31), limit=50,
    )
    assert len(bounded) == 50
    assert bounded[-1][0] == date(2024, 12, 31)
    assert bounded[0][0] >= date(2024, 3, 1)
    assert bounded_last == date(2024, 12, 31)
