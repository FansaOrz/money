"""规则模型筛选器测试：纯函数因子、五档落档、市场过滤、权重约束与 API。

使用合成的确定性净值序列，不依赖外部行情。
"""

from datetime import date, timedelta
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models import Account, FundNav, IndexQuote, Instrument, MarketIndex, Position
from app.schemas.quant import ScreenerRequest
from app.services import quant_factors as factors
from app.services import quant_screener as screener
from app.services.quant import QuantError


# ---------------------------------------------------------------------------
# 数据构造辅助
# ---------------------------------------------------------------------------


def _seed_navs(
    db: Session,
    code: str,
    name: str,
    days: int = 150,
    start_nav: float = 1.0,
    daily_growth: float = 0.001,
) -> Instrument:
    """写入一只基金及带交替噪声的趋势净值序列（daily_growth 可为负）。

    日收益 = daily_growth ± 0.1% 交替，保证日收益标准差 > 0，
    60 日风险调整动量可用，同时整体趋势由 daily_growth 决定。
    """
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
        noise = 0.001 if i % 2 == 0 else -0.001
        nav *= 1 + daily_growth + noise
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


def _seed_index(
    db: Session,
    code: str = "SH000001",
    name: str = "上证指数",
    market: str = "cn",
    days: int = 150,
    daily_growth: float = 0.001,
) -> MarketIndex:
    """写入一只指数及等比变化的收盘序列。"""
    index = MarketIndex(code=code, name=name, market=market, source_symbol=code.lower())
    db.add(index)
    db.flush()
    base = date(2025, 1, 1)
    price = 3000.0
    for i in range(days):
        db.add(
            IndexQuote(
                index_id=index.id,
                trade_date=base + timedelta(days=i),
                close=Decimal(f"{price:.4f}"),
            )
        )
        price *= 1 + daily_growth
    db.commit()
    return index


def _trend_series(days: int = 100, daily_growth: float = 0.002) -> list[float]:
    """生成等比净值序列（纯函数测试用）。"""
    values = [1.0]
    for _ in range(days - 1):
        values.append(values[-1] * (1 + daily_growth))
    return values


def _seed_pool(
    db: Session, count: int, *, name_prefix: str = "沪深", code_prefix: str = "10"
) -> list[Instrument]:
    """批量写入同一市场、增长单调的基金池（第 0 只增长最强，末位最弱）。"""
    instruments = []
    for i in range(count):
        instruments.append(
            _seed_navs(
                db,
                code=f"{code_prefix}{i:04d}",
                name=f"{name_prefix}基金{i}",
                days=150,
                daily_growth=0.004 - i * 0.001,
            )
        )
    return instruments


# ---------------------------------------------------------------------------
# 纯函数：市场分类
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("广发纳斯达克100ETF联接", "us_nasdaq"),
        ("博时标普500ETF", "us_spx"),
        ("美国REIT精选", "us_spx"),
        ("恒生科技指数ETF", "hk_tech"),
        ("华夏沪港通恒生ETF", "hk"),
        ("港股通精选混合", "hk"),
        ("易方达沪深300ETF", "cn_300"),
        ("易方达消费行业股票", "cn"),
        ("国泰黄金ETF", "gold"),
        ("招商产业债券A", "bond"),
        ("天弘余额宝货币", "money"),
        ("上投摩根全球新兴市场", "overseas"),
        ("德国DAX指数", "overseas"),
    ],
)
def test_classify_market(name: str, expected: str) -> None:
    """市场分类：按名称关键词有序匹配，A股兜底。"""
    assert factors.classify_market(name) == expected


def test_equity_market_membership() -> None:
    """观察池（黄金/债券/货币/其他海外）不参与横截面排名。"""
    for market in ("gold", "bond", "money", "overseas"):
        assert not factors.is_equity_market(market)
    for market in ("cn", "cn_300", "hk", "hk_tech", "us_spx", "us_nasdaq"):
        assert factors.is_equity_market(market)


# ---------------------------------------------------------------------------
# 纯函数：因子
# ---------------------------------------------------------------------------


def test_momentum_score_weights_and_windows() -> None:
    """动量 = 0.5×R20+0.3×R60+0.2×R120；等比序列可精确断言。"""
    values = _trend_series(days=150, daily_growth=0.001)
    momentum, detail = factors.momentum_score(values)
    expected = (
        0.5 * (1.001**20 - 1) + 0.3 * (1.001**60 - 1) + 0.2 * (1.001**120 - 1)
    )
    assert momentum == pytest.approx(expected, rel=1e-9)
    assert detail["r20"] == pytest.approx(1.001**20 - 1, rel=1e-9)
    assert detail["r60"] == pytest.approx(1.001**60 - 1, rel=1e-9)
    assert detail["r120"] == pytest.approx(1.001**120 - 1, rel=1e-9)


def test_momentum_score_renormalizes_when_window_missing() -> None:
    """样本不足 120 窗口时按 0.5/0.3 重新归一化。"""
    values = _trend_series(days=70, daily_growth=0.002)
    momentum, detail = factors.momentum_score(values)
    expected = (0.5 * (1.002**20 - 1) + 0.3 * (1.002**60 - 1)) / 0.8
    assert momentum == pytest.approx(expected, rel=1e-9)
    assert "r120" not in detail


def test_momentum_score_insufficient_samples() -> None:
    """样本不足 20 窗口时动量为 None。"""
    assert factors.momentum_score(_trend_series(days=10))[0] is None


def _noisy_series(days: int, drift: float) -> list[float]:
    """带交替噪声的趋势序列（日收益标准差 > 0）。"""
    values = [1.0]
    for i in range(days - 1):
        noise = 0.001 if i % 2 == 0 else -0.001
        values.append(values[-1] * (1 + drift + noise))
    return values


def test_risk_adjusted_momentum_direction() -> None:
    """风险调整动量：上涨为正、下跌为负；恒定序列（std=0）为 None。"""
    up = factors.risk_adjusted_momentum(_noisy_series(days=100, drift=0.002))
    down = factors.risk_adjusted_momentum(_noisy_series(days=100, drift=-0.002))
    assert up is not None and up > 0
    assert down is not None and down < 0
    # 日收益恒定的等比序列：标准差为 0 → None
    assert factors.risk_adjusted_momentum(_trend_series(days=100, daily_growth=0.002)) is None
    constant = [2.0] * 100
    assert factors.risk_adjusted_momentum(constant) is None


def test_trend_strength_bull_bear_flat() -> None:
    """趋势：多头排列 +1、空头排列 -1、无均线样本 None。"""
    bull = _trend_series(days=100, daily_growth=0.003)
    trend, evidence = factors.trend_strength(bull)
    assert trend == pytest.approx(1.0)
    assert evidence["ma20"] is not None and evidence["ma60"] is not None
    assert evidence["price"] > evidence["ma20"] > evidence["ma60"]

    bear = _trend_series(days=100, daily_growth=-0.003)
    trend, _ = factors.trend_strength(bear)
    assert trend == pytest.approx(-1.0)

    trend, _ = factors.trend_strength(_trend_series(days=10))
    assert trend is None  # MA20 都不足

    # 短样本有 MA20 无 MA60：按两项归一化
    trend, evidence = factors.trend_strength(_trend_series(days=30, daily_growth=0.002))
    assert trend == pytest.approx(1.0)
    assert evidence["ma60"] is None


def test_max_drawdown_window() -> None:
    """120 日回撤只取尾部窗口；先涨后跌序列可精确断言。"""
    up = _trend_series(days=60, daily_growth=0.01)
    down = _trend_series(days=61, daily_growth=-0.01)
    values = up + [up[-1] * d for d in [v / 1.0 for v in down[1:]]]
    # 末段 60 区间等比 -1%：回撤 = 0.99^60 - 1
    dd = factors.max_drawdown(values, window=120)
    assert dd == pytest.approx(0.99**60 - 1, rel=1e-6)
    assert factors.max_drawdown([1.0]) is None


# ---------------------------------------------------------------------------
# 纯函数：横截面统计
# ---------------------------------------------------------------------------


def test_zscores_basic() -> None:
    """z-score：均值 0、总体标准差 1；None 透传。"""
    result = factors.zscores({"a": 1.0, "b": 2.0, "c": 3.0, "d": None})
    assert result["a"] == pytest.approx(-1.224744871391589, rel=1e-9)
    assert result["b"] == pytest.approx(0.0, abs=1e-12)
    assert result["c"] == pytest.approx(1.224744871391589, rel=1e-9)
    assert result["d"] is None


def test_zscores_degenerate_cases() -> None:
    """单样本/零方差时有效值一律为 0。"""
    assert factors.zscores({"a": 5.0})["a"] == 0.0
    result = factors.zscores({"a": 2.0, "b": 2.0, "c": None})
    assert result["a"] == 0.0
    assert result["b"] == 0.0
    assert result["c"] is None


def test_quantile_ranks_boundaries() -> None:
    """分位数：单调序列名次 = i/n；并列取平均秩；None 透传。"""
    ranks = factors.quantile_ranks({str(i): float(i) for i in range(10)})
    for i in range(10):
        assert ranks[str(i)] == pytest.approx(i / 10)
    tied = factors.quantile_ranks({"a": 1.0, "b": 1.0, "c": 2.0})
    assert tied["a"] == pytest.approx(tied["b"])
    assert tied["c"] == pytest.approx(2 / 3)
    none_case = factors.quantile_ranks({"a": 1.0, "b": None})
    assert none_case["b"] is None


def test_composite_score_weights() -> None:
    """综合分 = 0.45×z(MOM)+0.35×z(RAM)+0.20×趋势+0.50×z(回撤)；None 按 0。"""
    score = factors.composite_score(1.0, 1.0, 1.0, 1.0)
    assert score == pytest.approx(0.45 + 0.35 + 0.20 + 0.50)
    assert factors.composite_score(None, None, None, None) == 0.0
    assert factors.composite_score(2.0, None, 0.5, None) == pytest.approx(0.45 * 2.0 + 0.20 * 0.5)


# ---------------------------------------------------------------------------
# 纯函数：五档落档与市场状态
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("quantile", "trend", "expected"),
    [
        (0.95, 1.0, 2),     # 前10% 且趋势不弱 → +2
        (0.90, 0.5, 2),     # 边界 0.9 属于前10%
        (0.95, 0.0, 1),     # 前10% 但趋势中性 → 回落 +1
        (0.95, -0.5, 1),    # 前10% 但趋势偏弱 → 回落 +1
        (0.85, 1.0, 1),     # 70%~90% → +1（趋势再好也不升 +2）
        (0.70, -1.0, 1),    # 边界 0.7 → +1
        (0.69, 1.0, 0),
        (0.50, 0.0, 0),     # 30%~70% → 0
        (0.30, -1.0, 0),    # 边界 0.3 → 0
        (0.29, 1.0, -1),
        (0.15, 0.0, -1),    # 10%~30% → −1
        (0.10, 1.0, -1),    # 边界 0.1 → −1
        (0.05, -1.0, -2),   # 后10% 且趋势不强 → −2
        (0.05, 0.0, -1),    # 后10% 但趋势中性 → 回落 −1
        (0.05, 0.5, -1),    # 后10% 但趋势偏强 → 回落 −1
        (None, 1.0, 0),     # 分位数缺失 → 中性
    ],
)
def test_tier_from_quantile_boundaries(
    quantile: float | None, trend: float | None, expected: int
) -> None:
    """五档边界：10%/30%/70%/90% 与趋势配合。"""
    assert factors.tier_from_quantile(quantile, trend) == expected


def test_tier_labels() -> None:
    """五档中文标签完整。"""
    assert factors.tier_label(2) == "值得研究加仓"
    assert factors.tier_label(1) == "偏积极"
    assert factors.tier_label(0) == "中性持有"
    assert factors.tier_label(-1) == "偏谨慎"
    assert factors.tier_label(-2) == "值得研究减仓"


@pytest.mark.parametrize(
    ("tier", "regime", "expected"),
    [
        (2, "risk_off", 1),
        (1, "risk_off", 0),
        (0, "risk_off", 0),
        (-1, "risk_off", -1),   # 负向信号不加强
        (-2, "risk_off", -2),
        (2, "neutral", 2),      # 非 risk_off 不调整
        (2, "risk_on", 2),
        (2, "insufficient", 2),
    ],
)
def test_adjust_tier_for_regime(tier: int, regime: str, expected: int) -> None:
    """Risk-off 正信号降一档，负向不加强，其余状态不动。"""
    assert factors.adjust_tier_for_regime(tier, regime) == expected


def test_index_regime_states() -> None:
    """指数状态：上涨趋势 risk_on、持续阴跌 risk_off、样本不足 insufficient。"""
    up = _trend_series(days=100, daily_growth=0.002)
    regime, evidence = factors.index_regime(up)
    assert regime == "risk_on"
    assert evidence["ma20"] is not None and evidence["ma60"] is not None

    down = _trend_series(days=100, daily_growth=-0.004)  # 20 日动量约 -7.7%
    regime, evidence = factors.index_regime(down)
    assert regime == "risk_off"
    assert evidence["momentum_20d"] < -0.05

    regime, _ = factors.index_regime(_trend_series(days=40))
    assert regime == "insufficient"


def test_index_regime_neutral() -> None:
    """多空交织的指数（先涨后横盘）落 neutral。"""
    up = _trend_series(days=80, daily_growth=0.003)
    flat = [up[-1]] * 40
    regime, _ = factors.index_regime(up + flat)
    assert regime == "neutral"


# ---------------------------------------------------------------------------
# 服务层：候选池与样本门槛
# ---------------------------------------------------------------------------


def test_screener_empty_portfolio_returns_400(client: TestClient) -> None:
    """无持仓且未指定 codes：400。"""
    response = client.get("/api/quant/screener/signals")
    assert response.status_code == 400
    assert "持仓" in response.json()["detail"]


def test_screener_excludes_insufficient_samples(
    client: TestClient, db_session: Session
) -> None:
    """净值样本 <120 的基金被剔除并计数。"""
    enough = _seed_navs(db_session, code="110001", name="沪深基金A", days=150)
    short = _seed_navs(db_session, code="110002", name="沪深基金B", days=60)
    _seed_position(db_session, enough)
    _seed_position(db_session, short)

    response = client.get("/api/quant/screener/signals")
    assert response.status_code == 200
    data = response.json()
    assert data["candidate_count"] == 1
    assert data["excluded_count"] == 1
    assert {item["code"] for item in data["items"]} == {"110001"}


def test_screener_observe_pool_not_ranked(client: TestClient, db_session: Session) -> None:
    """黄金/债券等观察池资产计入 observe_count，不参与排名。"""
    equity = _seed_navs(db_session, code="110001", name="沪深基金", days=150)
    gold = _seed_navs(db_session, code="110002", name="国泰黄金ETF", days=150)
    _seed_position(db_session, equity)
    _seed_position(db_session, gold)

    data = client.get("/api/quant/screener/signals").json()
    assert data["candidate_count"] == 1
    assert data["observe_count"] == 1
    assert {item["code"] for item in data["items"]} == {"110001"}
    assert all(item["market"] != "gold" for item in data["items"])


def test_screener_only_observe_pool(client: TestClient, db_session: Session) -> None:
    """候选均为观察池：不报 500，返回空入选与提示。"""
    gold = _seed_navs(db_session, code="110002", name="国泰黄金ETF", days=150)
    _seed_position(db_session, gold)

    response = client.get("/api/quant/screener/signals")
    assert response.status_code == 200
    data = response.json()
    assert data["candidate_count"] == 0
    assert data["observe_count"] == 1
    assert data["items"] == []
    assert any("观察池" in w for w in data["warnings"])


def test_screener_explicit_codes_and_missing(
    client: TestClient, db_session: Session
) -> None:
    """POST 显式指定 codes：未知代码跳过并提示；全部未知 400。"""
    _seed_navs(db_session, code="110001", name="沪深基金A", days=150)

    response = client.post(
        "/api/quant/screener/run", json={"codes": ["110001", "999999"]}
    )
    assert response.status_code == 200
    data = response.json()
    assert data["candidate_count"] == 1
    assert any("999999" in w for w in data["warnings"])

    response = client.post("/api/quant/screener/run", json={"codes": ["999999"]})
    assert response.status_code == 400


def test_screener_post_without_positions_uses_codes(
    client: TestClient, db_session: Session
) -> None:
    """无持仓时 POST 显式 codes 仍可运行。"""
    _seed_navs(db_session, code="110001", name="沪深基金A", days=150)
    response = client.post("/api/quant/screener/run", json={"codes": ["110001"]})
    assert response.status_code == 200
    assert response.json()["candidate_count"] == 1


# ---------------------------------------------------------------------------
# 服务层：排序方向与响应字段
# ---------------------------------------------------------------------------


def test_screener_ranking_direction(client: TestClient, db_session: Session) -> None:
    """恒涨基金综合分高于恒跌基金，字段完整。"""
    up = _seed_navs(db_session, code="110001", name="沪深恒涨基金", days=150, daily_growth=0.004)
    down = _seed_navs(db_session, code="110002", name="沪深恒跌基金", days=150, daily_growth=-0.004)
    flat = _seed_navs(db_session, code="110003", name="沪深平稳基金", days=150, daily_growth=0.0)
    for instrument in (up, down, flat):
        _seed_position(db_session, instrument)

    response = client.get("/api/quant/screener/signals")
    assert response.status_code == 200
    data = response.json()
    assert data["candidate_count"] == 3
    assert data["as_of"] == "2025-05-30"  # 2025-01-01 + 149 天
    assert data["methodology"]

    by_code = {item["code"]: item for item in data["items"]}
    assert by_code["110001"]["score"] > by_code["110003"]["score"] > by_code["110002"]["score"]
    # 候选池 <10 只：分位数不展示（None），全部中性档
    assert by_code["110001"]["quantile"] is None
    assert all(item["tier"] == 0 for item in data["items"])

    item = by_code["110001"]
    # 响应字段完整性：code/name/market/benchmark/score/quantile/tier/label/
    # target_weight/reasons/factors/data_date/warnings
    assert item["name"] == "沪深恒涨基金"
    assert item["market"] == "cn"
    assert item["benchmark"] == "SH000001"
    assert isinstance(item["score"], float)
    assert item["label"] == factors.tier_label(item["tier"])
    assert 0.0 <= item["target_weight"] <= 0.25 + 1e-9
    assert item["data_date"] == "2025-05-30"
    assert item["reasons"]
    assert set(item["factors"]) == {
        "momentum",
        "risk_adjusted_momentum_60d",
        "trend",
        "drawdown_120d",
    }
    assert item["factors"]["momentum"] > 0
    assert item["factors"]["trend"] == pytest.approx(1.0)
    assert item["factors"]["risk_adjusted_momentum_60d"] is not None
    assert isinstance(item["warnings"], list)
    # 候选数 ≤ top_n：全部进入目标组合
    assert all(i["in_target"] for i in data["items"])
    assert data["allocation_count"] == data["selected_count"] == 3


def test_screener_small_pool_all_neutral(client: TestClient, db_session: Session) -> None:
    """候选池 <10 只：样本不足，全部落中性档并提示。"""
    instruments = _seed_pool(db_session, 3)
    for instrument in instruments:
        _seed_position(db_session, instrument)

    data = client.get("/api/quant/screener/signals").json()
    assert data["candidate_count"] == 3
    assert all(item["tier"] == 0 for item in data["items"])
    assert all(item["label"] == "中性持有" for item in data["items"])
    assert any("样本不足" in w for w in data["warnings"])
    # 但分数与分位数仍计算（前端可展示相对强弱）
    scores = [item["score"] for item in data["items"]]
    assert len(set(scores)) > 1


def test_screener_ten_pool_full_tiers(client: TestClient, db_session: Session) -> None:
    """10 只单调强弱基金池：五档完整出现 +2/+1/0/−1/−2。"""
    instruments = _seed_pool(db_session, 10)
    for instrument in instruments:
        _seed_position(db_session, instrument)

    data = client.get("/api/quant/screener/signals").json()
    assert data["candidate_count"] == 10
    tiers = sorted(item["tier"] for item in data["items"])
    # 分位数 0.0~0.9 单调分布 + 趋势与增长同号：
    # 最强 +2、次强两只 +1、中间四只 0、其后两只 −1、最弱 −2
    assert tiers == [-2, -1, -1, 0, 0, 0, 0, 1, 1, 2]
    by_tier = {item["tier"]: item for item in data["items"] if item["tier"] in (2, -2)}
    assert by_tier[2]["label"] == "值得研究加仓"
    assert by_tier[-2]["label"] == "值得研究减仓"
    # 最强的是日增长最高的 100000
    assert by_tier[2]["code"] == "100000"


# ---------------------------------------------------------------------------
# 服务层：市场状态过滤
# ---------------------------------------------------------------------------


def test_screener_risk_off_demotes_positive_tiers(
    client: TestClient, db_session: Session
) -> None:
    """Risk-off 市场：正信号降一档（+2→+1、+1→0），负向不加强。"""
    instruments = _seed_pool(db_session, 10)
    for instrument in instruments:
        _seed_position(db_session, instrument)
    _seed_index(db_session, daily_growth=-0.004)  # 上证指数持续阴跌 → risk_off

    data = client.get("/api/quant/screener/signals").json()
    tiers = sorted(item["tier"] for item in data["items"])
    # 原 [-2,-1,-1,0,0,0,0,1,1,2] → risk-off 后 [-2,-1,-1,0,0,0,0,0,0,1]
    assert tiers == [-2, -1, -1, 0, 0, 0, 0, 0, 0, 1]
    demoted = [
        item for item in data["items"] if any("Risk-off" in r for r in item["reasons"])
    ]
    assert len(demoted) == 3  # 原 +2 与两只 +1


def test_screener_risk_on_keeps_tiers(client: TestClient, db_session: Session) -> None:
    """Risk-on 市场：信号不调整。"""
    instruments = _seed_pool(db_session, 10)
    for instrument in instruments:
        _seed_position(db_session, instrument)
    _seed_index(db_session, daily_growth=0.002)

    data = client.get("/api/quant/screener/signals").json()
    tiers = sorted(item["tier"] for item in data["items"])
    assert tiers == [-2, -1, -1, 0, 0, 0, 0, 1, 1, 2]


def test_screener_insufficient_index_history_no_adjustment(
    client: TestClient, db_session: Session
) -> None:
    """指数历史 <60 日：不调整信号并提示历史不足，不报 500。"""
    instruments = _seed_pool(db_session, 10)
    for instrument in instruments:
        _seed_position(db_session, instrument)
    _seed_index(db_session, days=40, daily_growth=-0.01)  # 明显下跌但历史不足

    data = client.get("/api/quant/screener/signals").json()
    tiers = sorted(item["tier"] for item in data["items"])
    assert tiers == [-2, -1, -1, 0, 0, 0, 0, 1, 1, 2]  # 与无过滤一致
    assert any("历史不足" in w for w in data["warnings"])


def test_screener_missing_index_table_data_no_500(
    client: TestClient, db_session: Session
) -> None:
    """指数表无该市场指数：不调整信号并提示，不报 500。"""
    instruments = _seed_pool(db_session, 10)
    for instrument in instruments:
        _seed_position(db_session, instrument)
    # 不写入任何指数行情

    response = client.get("/api/quant/screener/signals")
    assert response.status_code == 200
    data = response.json()
    assert any("指数" in w for w in data["warnings"])
    tiers = sorted(item["tier"] for item in data["items"])
    assert tiers == [-2, -1, -1, 0, 0, 0, 0, 1, 1, 2]


def test_screener_market_specific_regime(client: TestClient, db_session: Session) -> None:
    """多市场：仅 Risk-off 市场的正信号被降档，其他市场不受影响。

    通过 POST 显式 codes（top_n=10 覆盖两个市场各 10 只候选），
    避免默认持仓池下 top_n=10 只保留全池前 10 名而丢掉另一市场。
    """
    cn = _seed_pool(db_session, 10, name_prefix="沪深", code_prefix="10")
    us = _seed_pool(db_session, 10, name_prefix="纳斯达克", code_prefix="20")
    for instrument in cn + us:
        _seed_position(db_session, instrument)
    _seed_index(db_session, code="SH000001", name="上证指数", market="cn", daily_growth=-0.004)
    _seed_index(db_session, code="IXIC", name="纳斯达克", market="us", daily_growth=0.002)
    codes = [instrument.code for instrument in cn + us]

    data = client.post(
        "/api/quant/screener/run", json={"codes": codes, "top_n": 10}
    ).json()
    assert data["candidate_count"] == 20
    # 全部 20 只候选都参与分析；仅前 top_n 只进入目标组合
    assert data["selected_count"] == 20
    assert data["allocation_count"] == 10
    cn_items = [item for item in data["items"] if item["market"] == "cn"]
    us_items = [item for item in data["items"] if item["market"] == "us_nasdaq"]
    # 跨市场横截面：cn 恒跌指数不影响基金分数本身；进入目标组合的 10 只为分数最高者。
    # cn 侧 Risk-off：正信号被降档（不存在 +2），理由含降档说明；
    # us 侧 Risk-on：不调整，保留 +2。
    assert all(item["tier"] <= 1 for item in cn_items)
    assert any(
        any("Risk-off" in reason for reason in item["reasons"]) for item in cn_items
    )
    assert not any(
        any("Risk-off" in reason for reason in item["reasons"]) for item in us_items
    )
    assert all(item["benchmark"] == "IXIC" for item in us_items)


def test_screener_regime_applied_per_market_via_full_pool(
    client: TestClient, db_session: Session
) -> None:
    """单市场 20 只候选：top_n=10 入选后仍验证 Risk-off 降档作用于正信号。"""
    instruments = _seed_pool(db_session, 10, name_prefix="沪深", code_prefix="10")
    instruments += _seed_pool(db_session, 10, name_prefix="中证", code_prefix="30")
    for instrument in instruments:
        _seed_position(db_session, instrument)
    _seed_index(db_session, code="SH000001", name="上证指数", market="cn", daily_growth=-0.004)

    data = client.get("/api/quant/screener/signals").json()
    assert data["candidate_count"] == 20
    # 所有入选者都不存在 +2（risk-off 市场正信号最多 +1）
    assert all(item["tier"] <= 1 for item in data["items"])
    assert any(
        any("Risk-off" in reason for reason in item["reasons"]) for item in data["items"]
    )


# ---------------------------------------------------------------------------
# 服务层：权重约束
# ---------------------------------------------------------------------------


def test_screener_single_fund_weight_cap(client: TestClient, db_session: Session) -> None:
    """单基金目标权重 ≤25%，被截断时保留现金并提示。"""
    instrument = _seed_navs(db_session, code="110001", name="沪深基金", days=150)
    _seed_position(db_session, instrument)

    data = client.get("/api/quant/screener/signals").json()
    item = data["items"][0]
    assert item["target_weight"] == pytest.approx(0.25)
    assert any("截断" in w for w in item["warnings"])


def test_screener_market_weight_cap(client: TestClient, db_session: Session) -> None:
    """单一市场合计目标权重 ≤50%：3 只强势 cn 基金 + 1 只弱势 us 基金。"""
    # 3 只 cn 基金 + 1 只 us 基金，cn 侧理论权重远超 50%
    cn_instruments = _seed_pool(db_session, 3, name_prefix="沪深", code_prefix="10")
    us = _seed_navs(db_session, code="200000", name="纳斯达克基金", days=150, daily_growth=0.0005)
    for instrument in cn_instruments + [us]:
        _seed_position(db_session, instrument)

    data = client.get("/api/quant/screener/signals").json()
    cn_weight = sum(item["target_weight"] for item in data["items"] if item["market"] == "cn")
    assert cn_weight <= 0.50 + 1e-9
    for item in data["items"]:
        assert item["target_weight"] <= 0.25 + 1e-9


def test_screener_top_n_limit(client: TestClient, db_session: Session) -> None:
    """top_n 只限制目标组合配置数，全部候选仍参与分析并返回。"""
    instruments = _seed_pool(db_session, 10)
    codes = [instrument.code for instrument in instruments]

    response = client.post(
        "/api/quant/screener/run", json={"codes": codes, "top_n": 4}
    )
    assert response.status_code == 200
    data = response.json()
    assert data["candidate_count"] == 10
    # selected_count 为全部分析数，allocation_count 为目标组合配置数
    assert data["selected_count"] == 10
    assert data["allocation_count"] == 4
    assert len(data["items"]) == 10
    # 综合分最高的 4 只进入目标组合，其余仅分析
    in_target = [item for item in data["items"] if item["in_target"]]
    analysis_only = [item for item in data["items"] if not item["in_target"]]
    assert {item["code"] for item in in_target} == set(codes[:4])
    assert len(analysis_only) == 6
    # 目标组合内至少一只获得非零权重（单基金 25% 上限可能把后排截断为 0）
    assert sum(item["target_weight"] for item in in_target) > 0
    assert all(item["target_weight"] >= 0 for item in in_target)
    assert all(item["target_weight"] == 0 for item in analysis_only)
    # 未进入目标组合的候选也保留完整分析字段（五档/分位数/理由）
    assert all("tier" in item and "reasons" in item for item in analysis_only)
    assert any("仅参与分析" in w for w in data["warnings"])

    # top_n 超过 10 被 schema 拒绝
    response = client.post(
        "/api/quant/screener/run", json={"codes": codes, "top_n": 11}
    )
    assert response.status_code == 422


def test_screener_weights_sum_capped(client: TestClient, db_session: Session) -> None:
    """目标权重合计 ≤100%；截断部分保留为现金（合计 <100%）。

    10 只全为同一市场：单一市场 50% 上限先于单基金 25% 触顶，
    多轮再分配后市场合计 = 50%，单基金均 ≤25%。
    """
    instruments = _seed_pool(db_session, 10)
    for instrument in instruments:
        _seed_position(db_session, instrument)

    data = client.get("/api/quant/screener/signals").json()
    total = sum(item["target_weight"] for item in data["items"])
    assert 0.0 < total <= 1.0 + 1e-9
    # 市场合计被 50% 截断 → 合计 < 100%，截断部分保留为现金
    assert total < 1.0
    assert total == pytest.approx(0.50)
    assert all(item["target_weight"] <= 0.25 + 1e-9 for item in data["items"])
    # 综合分最强者权重最大（多轮再分配保持相对强弱次序）
    assert data["items"][0]["target_weight"] == max(
        item["target_weight"] for item in data["items"]
    )


def test_screener_items_sorted_by_weight(client: TestClient, db_session: Session) -> None:
    """目标组合标的按目标权重降序排在前，仅分析标的（权重 0）按综合分降序在后。"""
    instruments = _seed_pool(db_session, 10, name_prefix="沪深", code_prefix="10")
    instruments += _seed_pool(db_session, 5, name_prefix="中证", code_prefix="30")
    codes = [instrument.code for instrument in instruments]

    data = client.post(
        "/api/quant/screener/run", json={"codes": codes, "top_n": 4}
    ).json()
    assert data["allocation_count"] == 4
    assert data["selected_count"] == 15
    weights = [item["target_weight"] for item in data["items"]]
    # 前 4 只为目标组合（权重降序，受 25% 上限截断可能为 0），其余仅分析权重为 0
    assert weights[:4] == sorted(weights[:4], reverse=True)
    assert weights[0] > 0
    assert all(w == 0 for w in weights[4:])
    assert [item["in_target"] for item in data["items"]] == [True] * 4 + [False] * 11
    # 仅分析部分按综合分降序
    tail_scores = [item["score"] for item in data["items"][4:]]
    assert tail_scores == sorted(tail_scores, reverse=True)


# ---------------------------------------------------------------------------
# 服务层：全量分析与 top_n 仅约束目标组合
# ---------------------------------------------------------------------------


def test_screener_returns_all_candidates_beyond_default_top_n(
    client: TestClient, db_session: Session
) -> None:
    """默认 top_n=10 的 GET 接口：候选超过 10 只时仍返回全部分析结果。"""
    instruments = _seed_pool(db_session, 12)
    for instrument in instruments:
        _seed_position(db_session, instrument)

    data = client.get("/api/quant/screener/signals").json()
    assert data["candidate_count"] == 12
    assert data["selected_count"] == 12
    assert data["allocation_count"] == 10
    assert len(data["items"]) == 12
    in_target = [item for item in data["items"] if item["in_target"]]
    analysis_only = [item for item in data["items"] if not item["in_target"]]
    assert len(in_target) == 10
    assert len(analysis_only) == 2
    # 未进入目标组合的是综合分最低的两只，仅分析、权重为 0
    assert {item["code"] for item in analysis_only} == {"100010", "100011"}
    assert all(item["target_weight"] == 0 for item in analysis_only)
    # 五档分析不受 top_n 影响：仅分析标的仍带五档与分位数
    assert all(item["quantile"] is not None for item in data["items"])
    assert any("仅参与分析" in w for w in data["warnings"])
    # 权重约束仅作用于目标组合：合计 ≤100%，单基金 ≤25%
    total = sum(item["target_weight"] for item in in_target)
    assert 0.0 < total <= 1.0 + 1e-9
    assert all(item["target_weight"] <= 0.25 + 1e-9 for item in in_target)


def test_screener_analysis_only_items_keep_full_fields(
    client: TestClient, db_session: Session
) -> None:
    """仅分析标的保留完整分析字段（市场/基准/因子/数据日期），与目标组合一致。"""
    instruments = _seed_pool(db_session, 10)
    codes = [instrument.code for instrument in instruments]

    data = client.post(
        "/api/quant/screener/run", json={"codes": codes, "top_n": 3}
    ).json()
    assert data["allocation_count"] == 3
    analysis_only = [item for item in data["items"] if not item["in_target"]]
    assert len(analysis_only) == 7
    for item in analysis_only:
        assert item["market"] == "cn"
        assert item["benchmark"] == "SH000001"
        assert item["data_date"] == "2025-05-30"
        assert set(item["factors"]) == {
            "momentum",
            "risk_adjusted_momentum_60d",
            "trend",
            "drawdown_120d",
        }
        assert item["reasons"]
        assert item["quantile"] is not None  # 10 只候选：分位数照常展示


def test_screener_top_n_one_allocates_single_fund(
    client: TestClient, db_session: Session
) -> None:
    """top_n=1：仅综合分最高者进入目标组合（权重 25% 截断），其余仅分析。"""
    instruments = _seed_pool(db_session, 5)
    codes = [instrument.code for instrument in instruments]

    data = client.post(
        "/api/quant/screener/run", json={"codes": codes, "top_n": 1}
    ).json()
    assert data["selected_count"] == 5
    assert data["allocation_count"] == 1
    first = data["items"][0]
    assert first["code"] == codes[0]  # 增长最强者
    assert first["in_target"] is True
    assert first["target_weight"] == pytest.approx(0.25)  # 单基金上限截断
    assert any("截断" in w for w in first["warnings"])
    assert all(not item["in_target"] for item in data["items"][1:])


# ---------------------------------------------------------------------------
# 服务层：直接调用与确定性
# ---------------------------------------------------------------------------


def test_run_screener_service_directly(db_session: Session) -> None:
    """直接调用 run_screener：结构字段与计数正确。"""
    instruments = _seed_pool(db_session, 5)
    for instrument in instruments:
        _seed_position(db_session, instrument)

    result = screener.run_screener(db_session, ScreenerRequest())
    assert result.candidate_count == 5
    assert result.selected_count == 5
    assert result.allocation_count == 5
    assert result.excluded_count == 0
    assert result.as_of == "2025-05-30"
    assert all(item.tier == 0 for item in result.items)  # <10 只全部中性
    assert all(item.in_target for item in result.items)  # 候选 ≤ top_n 全部进入目标组合

    # top_n 只约束目标组合：全部分析数不变
    limited = screener.run_screener(db_session, ScreenerRequest(top_n=2))
    assert limited.selected_count == 5
    assert limited.allocation_count == 2
    assert sum(1 for item in limited.items if item.in_target) == 2
    assert sum(1 for item in limited.items if not item.in_target) == 3

    # min_samples 提高后样本不足被剔除
    strict = screener.run_screener(db_session, ScreenerRequest(min_samples=200))
    assert strict.candidate_count == 0
    assert strict.excluded_count == 5
    assert strict.allocation_count == 0
    assert strict.items == []


def test_screener_unknown_codes_all_raise(db_session: Session) -> None:
    """服务层：全部 codes 未知时抛 QuantError。"""
    with pytest.raises(QuantError):
        screener.run_screener(db_session, ScreenerRequest(codes=["999998", "999999"]))


def test_screener_result_deterministic(client: TestClient, db_session: Session) -> None:
    """相同数据两次调用结果完全一致（无随机性）。"""
    instruments = _seed_pool(db_session, 10)
    for instrument in instruments:
        _seed_position(db_session, instrument)
    _seed_index(db_session, daily_growth=-0.004)

    first = client.get("/api/quant/screener/signals").json()
    second = client.get("/api/quant/screener/signals").json()
    assert first == second


# ---------------------------------------------------------------------------
# 小样本市场分位回退与同分权重
# ---------------------------------------------------------------------------


def test_screener_single_member_market_not_forced_bottom_tier(
    client: TestClient, db_session: Session
) -> None:
    """单成员市场（有效样本 <5）：改用全权益池分位，不必然落末档。

    12 只强势 cn + 1 只弱势 hk + 3 只更弱 cn：hk 在全权益池（16 只）中
    分位 > 0（下方有 3 只更弱基金），落 −1 档而非单成员必然的分位 0/−2。
    """
    cn = _seed_pool(db_session, 12, name_prefix="沪深", code_prefix="10")
    hk = _seed_navs(db_session, code="200000", name="恒生港股基金", days=150, daily_growth=-0.004)
    weak = _seed_pool(db_session, 3, name_prefix="中证", code_prefix="30")
    # 让 30* 系列比 hk 更弱
    instruments = cn + [hk] + weak
    codes = [instrument.code for instrument in instruments]

    data = client.post(
        "/api/quant/screener/run", json={"codes": codes, "top_n": 10}
    ).json()
    by_code = {item["code"]: item for item in data["items"]}
    hk_item = by_code["200000"]
    # 单成员市场回退全权益池分位并给出提示
    assert any("全权益池分位" in w for w in hk_item["warnings"])
    assert hk_item["quantile"] is not None
    # hk（-0.004 增长）弱于 12 只强势 cn、但强于 3 只更弱 cn（-0.005 起）：
    # 分位 > 0，不再恒落末档
    assert hk_item["quantile"] > 0.0
    assert hk_item["tier"] > -2


def test_screener_tiny_pool_all_neutral_with_warning(
    client: TestClient, db_session: Session
) -> None:
    """全权益池 <5 只：不做分位，全部落中性档并提示样本不足。"""
    instruments = _seed_pool(db_session, 4)
    codes = [instrument.code for instrument in instruments]

    data = client.post(
        "/api/quant/screener/run", json={"codes": codes}
    ).json()
    assert data["candidate_count"] == 4
    assert all(item["tier"] == 0 for item in data["items"])
    assert any("样本不足" in w for w in data["warnings"])


def test_screener_tied_scores_weights_symmetric(db_session: Session) -> None:
    """同分候选（同市场）：多轮再分配后权重一致（同分同待遇），合计满足市场顶。"""
    # 构造 4 只净值序列完全相同的基金 → 综合分一致
    instruments = []
    for i in range(4):
        instruments.append(
            _seed_navs(db_session, code=f"40000{i}", name=f"沪深同款基金{i}", days=150, daily_growth=0.002)
        )
    codes = [instrument.code for instrument in instruments]

    result = screener.run_screener(db_session, ScreenerRequest(codes=codes))
    weights = [item.target_weight for item in result.items if item.in_target]
    assert len(weights) == 4
    # 同分 → 目标权重完全一致（多轮再分配同比例收敛）
    assert len({round(w, 6) for w in weights}) == 1
    # 4 只同市场：市场合计 ≤50%，单基金 ≤25%
    assert sum(weights) <= 0.50 + 1e-9
    assert all(w <= 0.25 + 1e-9 for w in weights)
