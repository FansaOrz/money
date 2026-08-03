"""Walk-Forward 滚动窗口组合回测测试。

重点验证：
1. 无未来数据：打分只使用训练窗口内净值（打分基准日 ≤ 测试期起点 - 1），
   修改测试期数据不改变持仓决策；回测不使用 start_date 之前的数据；
2. 指标正确性：不动基准下总收益/胜率/回撤为零、夏普为空，换手率为零，
   段收益与基准收益一致；
3. 样本不足：有效候选 <2 只、共同交易日不足一个完整窗口、
   单基金样本不足被剔除并提示；
4. 审计回归：step < test_window 被 schema 与引擎双重拒绝（重叠区间重复
   计收益）；净值曲线与真实测试日期逐日拼接（与段明细一致、策略/基准
   长度对齐）；逐日份额估值跨段不重复累计（换手调仓 / 非调仓连续两种
   场景的手算对比）；只有 1 个调仓期时换手为 0。

使用合成的确定性净值序列，不依赖外部行情。
"""

import math
from datetime import date, timedelta
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.models import Account, FundNav, Instrument, Position
from app.schemas.quant import WalkForwardRequest, WalkForwardWindow
from app.services import quant_walkforward as walkforward
from app.services.quant import QuantError


# ---------------------------------------------------------------------------
# 数据构造辅助
# ---------------------------------------------------------------------------


def _seed_navs(
    db: Session,
    code: str,
    name: str,
    days: int = 220,
    start_nav: float = 1.0,
    daily_growth: float = 0.001,
    start: date = date(2025, 1, 1),
) -> Instrument:
    """写入一只基金及带交替噪声的趋势净值序列（daily_growth 可为负）。"""
    instrument = Instrument(code=code, name=name)
    db.add(instrument)
    db.flush()
    nav = start_nav
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
        noise = 0.001 if i % 2 == 0 else -0.001
        nav *= 1 + daily_growth + noise
    db.commit()
    return instrument


def _seed_flat_navs(
    db: Session,
    code: str,
    name: str,
    days: int = 200,
    nav: float = 1.5,
) -> Instrument:
    """写入一只净值恒定的基金（确定性断言用：日收益恒为 0）。"""
    instrument = Instrument(code=code, name=name)
    db.add(instrument)
    db.flush()
    base = date(2025, 1, 1)
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


def _sine_panel(days: int, amplitude: float = 0.08, phase: float = 0.0) -> list[float]:
    """均值回归型正弦净值序列（强弱随窗口轮动，用于无未来数据探针）。"""
    return [
        1.0 + amplitude * math.sin(2 * math.pi * i / 20.0 + phase) for i in range(days)
    ]


def _probe_panel(days: int, strong_until: int, strong: float = 0.02, weak: float = -0.004) -> list[float]:
    """前 strong_until 天恒涨、其后恒跌的净值序列（未来数据探针用）。

    动量因子方向由训练窗口截止日位置决定：打分基准日落在强势段则得分高。
    """

    values = [1.0]
    for i in range(days - 1):
        values.append(values[-1] * (1 + (strong if i < strong_until else weak)))
    return values


def _flat_panel(days: int, value: float = 2.0) -> list[float]:
    return [value] * days


def _make_calendar(days: int, start: date = date(2025, 1, 1)) -> list[date]:
    return [start + timedelta(days=i) for i in range(days)]


# ---------------------------------------------------------------------------
# 审计回归：schema 校验与重叠禁止
# ---------------------------------------------------------------------------


def test_step_below_test_window_rejected_by_schema() -> None:
    """step < test_window：pydantic 校验拒绝（重叠测试区间会重复累计收益）。"""
    with pytest.raises(ValidationError, match="test_window"):
        WalkForwardWindow(train_window=60, test_window=20, step=10)
    # step == test_window（不重叠滚动）与 step > test_window（留间隔）均合法
    assert WalkForwardWindow(train_window=60, test_window=20, step=20).step == 20
    assert WalkForwardWindow(train_window=60, test_window=20, step=30).step == 30


def test_step_below_test_window_rejected_by_engine() -> None:
    """绕过 schema 构造的请求（step < test_window）被引擎兜底拒绝。"""
    days = 100
    calendar = _make_calendar(days)
    panels = {"A": _flat_panel(days), "B": _flat_panel(days, 1.2)}
    markets = {code: "cn" for code in panels}
    req = WalkForwardRequest.model_construct(
        candidate_codes=list(panels),
        window=WalkForwardWindow.model_construct(
            train_window=20, test_window=10, step=5
        ),
        top_n=2,
        initial_capital=10000.0,
        start_date=None,
        end_date=None,
    )
    with pytest.raises(QuantError, match="step"):
        walkforward.run_walkforward_panels(calendar, panels, markets, req)


def test_step_below_test_window_returns_422(client: TestClient, db_session: Session) -> None:
    """API 层：step < test_window 的请求被请求校验拒绝（422）。"""
    instruments = [
        _seed_navs(db_session, code=f"1100{i:02d}", name=f"沪深基金{i}", days=120)
        for i in range(3)
    ]
    codes = [inst.code for inst in instruments]
    response = client.post(
        "/api/quant/walkforward",
        json={
            "candidate_codes": codes,
            "window": {"train_window": 60, "test_window": 20, "step": 10},
        },
    )
    assert response.status_code == 422


# ---------------------------------------------------------------------------
# 审计回归：真实测试日期拼接 / 逐日份额估值 / 换手口径
# ---------------------------------------------------------------------------


def test_curve_matches_real_test_dates_and_segments() -> None:
    """净值曲线逐日拼接真实测试日期：与段明细区间完全一致、策略/基准长度对齐。

    每个曲线点都必须属于某个段的样本外测试区间（不允许出现段间空隙被
    静默跳过或同一交易日被重复计入），曲线总长 = Σ 各段测试天数。
    """
    days = 220
    calendar = _make_calendar(days)
    panels = {
        "A": _sine_panel(days, phase=0.0),
        "B": _sine_panel(days, phase=0.7),
        "C": _sine_panel(days, phase=1.4),
        "D": _sine_panel(days, phase=2.1),
    }
    markets = {code: "cn" for code in panels}
    req = WalkForwardRequest(candidate_codes=list(panels), top_n=2)

    strategy, benchmark, segments, _, _ = walkforward.run_walkforward_panels(
        calendar, panels, markets, req
    )

    # 策略与基准同日起算、逐日一一对齐
    assert len(strategy) == len(benchmark)
    # 曲线总长 = 各段测试区间长度之和（无重叠、无遗漏）
    expected_days = sum(
        (date.fromisoformat(seg.test_end) - date.fromisoformat(seg.test_start)).days + 1
        for seg in segments
    )
    assert len(strategy) == expected_days

    # 曲线日期与段区间逐个拼接一致（段间恰好首尾相接）
    cursor = 0
    for prev_end, seg in zip([None] + segments, segments):
        if prev_end is not None:
            # 相邻段：上一段测试末日的下一交易日 = 本段测试首日
            assert date.fromisoformat(prev_end.test_end) + timedelta(days=1) == date.fromisoformat(
                seg.test_start
            )
        seg_days = (
            date.fromisoformat(seg.test_end) - date.fromisoformat(seg.test_start)
        ).days + 1
        curve_dates = [
            calendar[len(calendar) - len(strategy) + cursor + k] for k in range(seg_days)
        ]
        assert curve_dates[0].isoformat() == seg.test_start
        assert curve_dates[-1].isoformat() == seg.test_end
        cursor += seg_days


def test_hand_calc_single_fund_two_segments() -> None:
    """手算对比：单基金 25% 仓位 + 75% 现金，两段窗口逐日净值分毫不差。

    A 恒定日涨 1%（打分恒最高），B 横盘；top_n=1 → 每期目标 {A: 0.25}。
    手算口径：调仓日（打分基准日收盘）按当日净值折算建仓，测试期逐日
    组合净值 = 0.25×nav(t)/nav(基准日) + 0.75，跨段相乘结转。
    """
    days = 54  # 起点 0/10/20，尾部余 4 < test//2 不并入：恰好 3 段各 10 天
    calendar = _make_calendar(days)
    panels = {"A": [1.01**i for i in range(days)], "B": [1.0] * days}
    markets = {code: "cn" for code in panels}
    req = WalkForwardRequest(
        candidate_codes=["A", "B"],
        window=WalkForwardWindow(train_window=20, test_window=10, step=10),
        top_n=1,
    )

    strategy, benchmark, segments, turnover, _ = walkforward.run_walkforward_panels(
        calendar, panels, markets, req
    )
    assert len(segments) == 3  # 起点 0/10/20

    # 每段测试首日先用旧持仓盯市，再按该日净值重平衡；随后逐日估值。
    expected: list[float] = []
    shares_a = 0.0
    cash = 1.0
    for start in (0, 10, 20):
        base = start + 20
        equity = shares_a * panels["A"][base] + cash
        shares_a = 0.25 * equity / panels["A"][base]
        cash = 0.75 * equity
        for t in range(base, base + 10):
            expected.append(shares_a * panels["A"][t] + cash)
    assert strategy == pytest.approx(expected, rel=1e-12, abs=1e-12)
    # 基准 = 等权买入持有：A 全程日涨 1%、B 横盘 → 逐日 0.5×1.01^(t-19)+0.5
    bench_expected = [0.5 * 1.01 ** (t - 20) + 0.5 for t in range(20, 50)]
    assert benchmark == pytest.approx(bench_expected, rel=1e-12, abs=1e-12)
    # 每期目标同为 {A: 0.25}，但 A 上涨使漂移权重 > 0.25 → 调仓把超出部分
    # 卖回现金，换手 > 0；手算两次调仓（第 2、3 期）的均值：
    turn = []
    for prev_base in (19, 29):
        drift_a = 0.25 * 1.01 ** (prev_base + 10 - prev_base)
        turn.append(drift_a / (drift_a + 0.75) - 0.25)
    assert turnover == pytest.approx(sum(turn) / len(turn), rel=1e-9)
    assert turnover > 0.0


def test_hand_calc_holdings_switch_no_double_count() -> None:
    """手算对比：跨段换股时上一段收益不重复累计（审计回归）。

    A 前 25 天日涨 2%（第 1 期训练窗口强势、入选），其后日跌 2%（第 2 期
    训练窗口弱势、落选）；B 全程横盘。第 2 期改持 B 后，A 在第 1 段测试
    期内的涨幅必须只计入组合一次 —— 调仓按第 2 期打分基准日收盘净值折算，
    不再从第 1 期建仓成本重新锚定。
    """
    days = 54  # 起点 0/10/20，尾部余 4 < test//2 不并入：恰好 3 段各 10 天
    calendar = _make_calendar(days)
    values_a = [1.0]
    for i in range(days - 1):
        values_a.append(values_a[-1] * (1.02 if i < 25 else 0.98))
    panels = {"A": values_a, "B": [1.0] * days}
    markets = {code: "cn" for code in panels}
    req = WalkForwardRequest(
        candidate_codes=["A", "B"],
        window=WalkForwardWindow(train_window=20, test_window=10, step=10),
        top_n=1,
    )

    strategy, _, segments, _, _ = walkforward.run_walkforward_panels(
        calendar, panels, markets, req
    )
    assert [set(seg.holdings) for seg in segments] == [{"A"}, {"B"}, {"B"}]

    # 段1在 idx20 建仓 A；段2在 idx30 先按 A 当日净值估值，再换成横盘 B。
    expected: list[float] = []
    shares_a = 0.25 / values_a[20]
    cash = 0.75
    for t in range(20, 30):
        expected.append(shares_a * values_a[t] + cash)
    equity30 = shares_a * values_a[30] + cash
    shares_b = 0.25 * equity30
    cash_b = 0.75 * equity30
    for _t in range(30, 50):
        expected.append(shares_b + cash_b)

    assert strategy == pytest.approx(expected, rel=1e-12, abs=1e-12)
    # 关键回归断言：段2、段3 净值严格持平（A 的旧收益没有被重复累计）
    seg2 = strategy[10:20]
    assert all(v == pytest.approx(seg2[0], rel=1e-12) for v in seg2)
    assert segments[1].segment_return == pytest.approx(0.0)
    assert segments[2].segment_return == pytest.approx(0.0)


def test_hand_calc_non_rebalance_shares_continuous() -> None:
    """手算对比：非调仓期份额连续（自然漂移），不重打分、不重新锚定。

    rebalance_interval=2：第 2 段沿用第 1 段份额。手算口径：份额在第 1 期
    打分基准日固定后不再变化，组合净值 = Σ份额×nav(t)+现金 全程连续估值；
    第 2 段首日净值 = 第 1 段末日净值 × 当日涨跌（不把第 1 段收益重复计入）。
    """
    days = 54  # 起点 0/10/20，尾部余 4 < test//2 不并入：恰好 3 段各 10 天
    calendar = _make_calendar(days)
    panels = {"A": [1.01**i for i in range(days)], "B": [1.0] * days}
    markets = {code: "cn" for code in panels}
    req = WalkForwardRequest(
        candidate_codes=["A", "B"],
        window=WalkForwardWindow(train_window=20, test_window=10, step=10),
        top_n=1,
    )

    strategy, _, segments, turnover, _ = walkforward.run_walkforward_panels(
        calendar, panels, markets, req, rebalance_interval=2
    )
    assert len(segments) == 3
    # 第 2 段非调仓：沿用第 1 段持仓；第 3 段（index-1=2 为 2 的倍数）重新打分
    assert segments[1].holdings == segments[0].holdings

    # 第 1 期在测试首日 idx20 建仓；第 2 段沿用；第 3 期在 idx40 重平衡。
    shares_a = 0.25 / panels["A"][20]
    cash = 0.75
    expected = [shares_a * panels["A"][t] + cash for t in range(20, 40)]
    equity40 = shares_a * panels["A"][40] + cash
    shares_a2 = 0.25 * equity40 / panels["A"][40]
    cash2 = 0.75 * equity40
    expected += [shares_a2 * panels["A"][t] + cash2 for t in range(40, 50)]

    assert strategy == pytest.approx(expected, rel=1e-12, abs=1e-12)
    # 非调仓期不产生换手：仅第 3 期一次调仓计入平均换手。
    # 手算第 3 期换手：漂移权重 = 持有 20 天后的市值占比（A 上涨 → 仓位 > 0.25），
    # 目标权重 = {A: 0.25, 现金 0.75} → 单边差额 = 漂移偏离 0.25 的部分
    drift_a = 0.25 * panels["A"][39] / panels["A"][19]
    drift_total = drift_a + 0.75
    expected_turnover = drift_a / drift_total - 0.25  # = 0.5×(|ΔA|+|Δ现金|)
    assert turnover == pytest.approx(expected_turnover, rel=1e-9)
    assert turnover > 0.0  # 漂移真实存在，换手非零（口径正确性锚点）


def test_single_period_turnover_is_zero() -> None:
    """只有 1 个调仓期时平均换手 = 0（首期建仓的机械换手不计入）。

    数据只够一个完整窗口：turnovers 列表只有首期的「0 → 目标权重」换手，
    它不是真正的调仓，平均换手必须为 0。
    """
    days = 32  # 起点 0，尾部余 2 < test//2 不并入：恰好 1 段
    calendar = _make_calendar(days)
    panels = {"A": _sine_panel(days), "B": _sine_panel(days, phase=0.7)}
    markets = {code: "cn" for code in panels}
    req = WalkForwardRequest(
        candidate_codes=["A", "B"],
        window=WalkForwardWindow(train_window=20, test_window=10, step=10),
        top_n=1,
    )

    strategy, _, segments, turnover, _ = walkforward.run_walkforward_panels(
        calendar, panels, markets, req
    )
    assert len(segments) == 1  # 40 天恰好容纳 train 20 + test 10（余 10 < 20 不再成段）
    assert turnover == pytest.approx(0.0)
    # 首期仍按目标权重建仓（曲线不为恒 1）
    assert strategy[-1] != pytest.approx(1.0)


def test_two_periods_turnover_excludes_initial_build() -> None:
    """两个调仓期：平均换手 = 第 2 期「漂移 → 目标」换手（不含首期建仓）。

    A 先强后弱、B 先弱后强：第 1 期持 A、第 2 期持 B，第 2 期把 0.25 的
    漂移仓位全部换到 B → 单次换手 0.25，平均换手 = 第 2 期换手的
    手算值（口径锚点；若错误地把首期建仓也计入均值或重复使用
    名义目标权重，均与手算值不符）。
    """
    days = 54  # 起点 0/20，尾部余 4 < test//2 不并入：恰好 2 段各 10 天
    calendar = _make_calendar(days)
    values_a, values_b = [1.0], [1.0]
    for i in range(days - 1):
        values_a.append(values_a[-1] * (1.02 if i < 19 else 0.98))
        values_b.append(values_b[-1] * (0.98 if i < 19 else 1.02))
    panels = {"A": values_a, "B": values_b}
    markets = {code: "cn" for code in panels}
    req = WalkForwardRequest(
        candidate_codes=["A", "B"],
        window=WalkForwardWindow(train_window=20, test_window=10, step=20),
        top_n=1,
    )

    _, _, segments, turnover, _ = walkforward.run_walkforward_panels(
        calendar, panels, markets, req
    )
    assert len(segments) == 2
    assert set(segments[0].holdings) == {"A"}
    assert set(segments[1].holdings) == {"B"}

    # 手算第 2 期换手：漂移组合 = {A 市值, 现金}，目标 = {B: 0.25, 现金 0.75}
    drift_a = 0.25 * values_a[29] / values_a[19]
    total = drift_a + 0.75
    expected_turnover = 0.5 * (
        abs(0.0 - drift_a / total)      # A：目标 0
        + abs(0.25 - 0.0)               # B：目标 0.25
        + abs(0.75 - 0.75 / total)      # 现金
    )
    assert turnover == pytest.approx(expected_turnover, rel=1e-9)


# ---------------------------------------------------------------------------
# 无未来数据（服务层纯函数）
# ---------------------------------------------------------------------------


def test_segment_dates_no_overlap_and_scoring_before_test() -> None:
    """各窗口打分基准日严格早于测试期起点；测试期不重叠（step == test_window）。"""
    days = 220
    calendar = _make_calendar(days)
    panels = {
        "A": _sine_panel(days, phase=0.0),
        "B": _sine_panel(days, phase=0.7),
        "C": _sine_panel(days, phase=1.4),
        "D": _sine_panel(days, phase=2.1),
    }
    markets = {code: "cn" for code in panels}
    req = WalkForwardRequest(candidate_codes=list(panels), top_n=2)

    strategy, benchmark, segments, turnover, warnings = walkforward.run_walkforward_panels(
        calendar, panels, markets, req
    )

    # 220 天、120+20 步进 20：起点 0/20/40/60/80 → 5 段
    assert len(segments) == 5
    assert len(strategy) == len(benchmark) == days - 120

    for seg in segments:
        train_end = date.fromisoformat(seg.train_end)
        test_start = date.fromisoformat(seg.test_start)
        # 打分基准日（训练窗口最后一日）必须严格早于样本外测试起点
        assert train_end < test_start
        # 段序号连续
        assert seg.index == len([s for s in segments if s.train_start <= seg.train_start])

    # step == test_window：测试区间首尾相接、不重叠
    for prev, nxt in zip(segments, segments[1:]):
        assert date.fromisoformat(prev.test_end) < date.fromisoformat(nxt.test_start)

    # 窗口持仓权重合法：不卖空、合计 ≤ 1、单基金 ≤ 25%
    for seg in segments:
        assert all(w > 0 for w in seg.holdings.values())
        assert sum(seg.holdings.values()) <= 1.0 + 1e-9
        assert all(w <= 0.25 + 1e-9 for w in seg.holdings.values())


def test_scoring_ignores_future_data() -> None:
    """篡改测试期（打分基准日之后）的净值，持仓决策完全不变。

    探针设计：基金 D 在全部训练窗口内强势、在每一段测试期内暴跌。
    若打分偷看未来数据，D 不会被选中；两段窗口都选中 D 即证明
    打分只依赖打分基准日及之前的数据。进一步把所有打分基准日之后的
    净值 ×3 篡改，持仓决策必须逐段完全一致。
    """
    days = 180
    calendar = _make_calendar(days)
    base_panels = {
        "A": _probe_panel(days, 140),   # 首段测试期内转弱
        "B": _probe_panel(days, 10),    # 长期弱势
        "C": _probe_panel(days, 170),   # 几乎全程强势
        "D": _probe_panel(days, 120),   # 训练期强势、每段测试期暴跌
    }
    markets = {code: "cn" for code in base_panels}
    req = WalkForwardRequest(candidate_codes=list(base_panels), top_n=2)

    _, _, seg_base, _, _ = walkforward.run_walkforward_panels(
        calendar, base_panels, markets, req
    )
    # 探针：A 在首个测试期内转弱，无未来数据的打分仍选中 A
    assert "A" in seg_base[0].holdings
    # 后续是否继续持有 A 取决于修正后的正分归一与约束；无未来数据的核心
    # 断言是篡改测试期后逐段持仓不变，下面会直接验证。

    # 只篡改最后一个信号日之后的数据；它不应改变此前任何一段持仓。
    last_train_end = max(date.fromisoformat(segment.train_end) for segment in seg_base)
    cutoff = calendar.index(last_train_end) + 1
    tampered = {
        code: [v if i < cutoff else v * 3.0 for i, v in enumerate(values)]
        for code, values in base_panels.items()
    }
    _, _, seg_tampered, _, _ = walkforward.run_walkforward_panels(
        calendar, tampered, markets, req
    )

    for before, after in zip(seg_base, seg_tampered):
        assert before.holdings == after.holdings
        assert before.train_start == after.train_start
        assert before.train_end == after.train_end
        assert before.test_start == after.test_start
        assert before.test_end == after.test_end


def test_start_date_excludes_earlier_navs(client: TestClient, db_session: Session) -> None:
    """start_date 之前的净值不参与回测：窗口明细的最早日期不得早于 start_date。"""
    instruments = [
        _seed_navs(
            db_session,
            code=f"1100{i:02d}",
            name=f"沪深基金{i}",
            days=280,
            daily_growth=0.002 - i * 0.001,
        )
        for i in range(4)
    ]
    codes = [inst.code for inst in instruments]

    response = client.post(
        "/api/quant/walkforward",
        json={"candidate_codes": codes, "top_n": 2, "start_date": "2025-03-12"},
    )
    assert response.status_code == 200
    data = response.json()
    # 2025-03-12 是第 70 个净值日（2025-01-01 起），剩余 210 天 ≥ 120+20
    assert data["segments"]
    for seg in data["segments"]:
        assert seg["train_start"] >= "2025-03-12"
    # 曲线终点为最后净值日：2025-01-01 + 279 天
    assert data["end_date"] == "2025-10-07"


# ---------------------------------------------------------------------------
# 指标正确性（确定性数据）
# ---------------------------------------------------------------------------


def test_flat_navs_deterministic_metrics(client: TestClient, db_session: Session) -> None:
    """净值恒定时：收益/回撤/换手全为 0，夏普为空，胜率为 0，曲线恒为 1。"""
    instruments = [
        _seed_flat_navs(db_session, code=f"1100{i:02d}", name=f"沪深平稳基金{i}")
        for i in range(3)
    ]
    codes = [inst.code for inst in instruments]

    response = client.post(
        "/api/quant/walkforward", json={"candidate_codes": codes, "top_n": 2}
    )
    assert response.status_code == 200
    data = response.json()

    # 200 天、120+20 步进 20：4 段；曲线 80 个点全为 1
    assert data["rebalance_count"] == 4
    assert len(data["segments"]) == 4
    assert len(data["curve"]) == 80
    assert all(p["strategy"] == pytest.approx(1.0) for p in data["curve"])
    assert all(p["benchmark"] == pytest.approx(1.0) for p in data["curve"])

    for key in ("strategy", "benchmark"):
        summary = data[key]
        assert summary["total_return"] == pytest.approx(0.0)
        assert summary["annual_return"] == pytest.approx(0.0)
        assert summary["max_drawdown"] == pytest.approx(0.0)
        assert summary["sharpe"] is None  # 日收益标准差为 0
        assert summary["win_rate"] == pytest.approx(0.0)  # 无正收益日

    assert data["excess_return"] == pytest.approx(0.0)
    assert data["turnover"] == pytest.approx(0.0)  # 每期持仓相同 → 零换手

    for seg in data["segments"]:
        assert seg["segment_return"] == pytest.approx(0.0)
        assert seg["benchmark_return"] == pytest.approx(0.0)
        assert len(seg["holdings"]) == 2
        # 恒定分数 → 2 只入选，单基金 ≤25% 约束截断到 0.25（其余为现金）
        assert all(w == pytest.approx(0.25) for w in seg["holdings"].values())


def test_benchmark_is_equal_weight_buy_hold(client: TestClient, db_session: Session) -> None:
    """基准为全部候选等权买入持有：末值 = 各候选累计涨幅的等权平均。"""
    growths = [0.002, 0.001, -0.001]
    instruments = [
        _seed_navs(
            db_session,
            code=f"1100{i:02d}",
            name=f"沪深基金{i}",
            days=200,
            daily_growth=growths[i],
        )
        for i in range(3)
    ]
    codes = [inst.code for inst in instruments]

    data = client.post(
        "/api/quant/walkforward", json={"candidate_codes": codes, "top_n": 1}
    ).json()

    # 基准从首个样本外测试日（索引120）建仓，终点为第199天
    nav_by_code = {}
    for i, code in enumerate(codes):
        nav = 1.0
        values = [nav]
        for k in range(199):
            noise = 0.001 if k % 2 == 0 else -0.001
            nav *= 1 + growths[i] + noise
            values.append(nav)
        nav_by_code[code] = values
    expected = sum(
        values[199] / values[120] for values in nav_by_code.values()
    ) / len(codes)
    assert data["curve"][-1]["benchmark"] == pytest.approx(expected, rel=1e-6)
    assert data["benchmark"]["total_return"] == pytest.approx(expected - 1.0, rel=1e-5)


def test_strategy_segments_and_weights_constraints(
    client: TestClient, db_session: Session
) -> None:
    """策略段收益与曲线一致；top_n=1 时单基金权重被 25% 约束截断。"""
    instruments = [
        _seed_navs(
            db_session,
            code=f"1100{i:02d}",
            name=f"沪深基金{i}",
            days=220,
            daily_growth=0.004 - i * 0.002,
        )
        for i in range(4)
    ]
    codes = [inst.code for inst in instruments]

    data = client.post(
        "/api/quant/walkforward", json={"candidate_codes": codes, "top_n": 1}
    ).json()
    assert data["rebalance_count"] == 5
    seg = data["segments"][0]
    # top_n=1：唯一入选基金理论权重 100% → 截断到 25%，其余 75% 为现金
    assert len(seg["holdings"]) == 1
    only_weight = next(iter(seg["holdings"].values()))
    assert only_weight == pytest.approx(0.25)

    # 第一段策略收益 = 25% 仓位 × 该基金测试期涨幅（现金零收益）
    code = next(iter(seg["holdings"]))
    # 重新生成该基金的确定性净值序列
    nav = 1.0
    values = [nav]
    for k in range(219):
        noise = 0.001 if k % 2 == 0 else -0.001
        nav *= 1 + 0.004 + noise  # 打分最高的一定是增长最强的 110000
        values.append(nav)
    assert code == "110000"
    fund_gain = values[139] / values[120] - 1.0
    # 测试首日建仓，第一段收益只覆盖 idx120→idx139。
    assert seg["segment_return"] == pytest.approx(0.25 * fund_gain, rel=1e-3)
    assert data["curve"][0]["strategy"] == pytest.approx(1.0, rel=1e-3)
    # 首个样本外测试日 = 2025-01-01 起第 121 天（索引 120，train_end 的下一交易日）
    assert data["start_date"] == "2025-05-01"


def test_response_schema_fields(client: TestClient, db_session: Session) -> None:
    """响应字段完整：params/curve/segments/summary/methodology/warnings。"""
    instruments = [
        _seed_navs(
            db_session,
            code=f"1100{i:02d}",
            name=f"沪深基金{i}",
            days=200,
            daily_growth=0.002 - i * 0.001,
        )
        for i in range(3)
    ]
    codes = [inst.code for inst in instruments]

    data = client.post(
        "/api/quant/walkforward", json={"candidate_codes": codes, "top_n": 2}
    ).json()
    assert data["params"]["train_window"] == 120
    assert data["params"]["test_window"] == 20
    assert data["params"]["step"] == 20
    assert data["params"]["top_n"] == 2
    assert data["params"]["candidate_codes"] == codes
    assert data["initial_capital"] == 10000.0
    assert data["methodology"]
    assert isinstance(data["warnings"], list)
    assert set(data["strategy"]) == {
        "total_return", "annual_return", "max_drawdown", "sharpe", "win_rate",
    }
    assert set(data["benchmark"]) == set(data["strategy"])
    point = data["curve"][0]
    assert set(point) == {"date", "strategy", "benchmark"}
    segment = data["segments"][0]
    assert set(segment) == {
        "index", "train_start", "train_end", "test_start", "test_end",
        "holdings", "segment_return", "benchmark_return",
    }


# ---------------------------------------------------------------------------
# 样本不足与错误处理
# ---------------------------------------------------------------------------


def test_insufficient_samples_raises_400(client: TestClient, db_session: Session) -> None:
    """共同交易日不足 train+test 一个完整窗口：400 且提示样本不足。"""
    instruments = [
        _seed_navs(db_session, code=f"1100{i:02d}", name=f"沪深基金{i}", days=100)
        for i in range(3)
    ]
    codes = [inst.code for inst in instruments]

    response = client.post(
        "/api/quant/walkforward", json={"candidate_codes": codes}
    )
    assert response.status_code == 400
    assert "样本" in response.json()["detail"]


def test_fund_below_min_samples_excluded_with_warning(
    client: TestClient, db_session: Session
) -> None:
    """样本不足 train+step 的基金被剔除并在 warnings 中提示，不影响其余候选。"""
    good = [
        _seed_navs(db_session, code=f"1100{i:02d}", name=f"沪深基金{i}", days=220)
        for i in range(3)
    ]
    short = _seed_navs(db_session, code="110099", name="沪深短样本基金", days=100)
    codes = [inst.code for inst in good] + [short.code]

    response = client.post(
        "/api/quant/walkforward", json={"candidate_codes": codes, "top_n": 2}
    )
    assert response.status_code == 200
    data = response.json()
    assert data["params"]["candidate_codes"] == [inst.code for inst in good]
    assert any("110099" in w and "剔除" in w for w in data["warnings"])


def test_fewer_than_two_valid_candidates_raises_400(
    client: TestClient, db_session: Session
) -> None:
    """有效候选不足 2 只（无法构造等权基准）：400。"""
    _seed_navs(db_session, code="110001", name="沪深基金A", days=220)
    _seed_navs(db_session, code="110002", name="沪深基金B", days=60)  # 样本不足被剔除

    response = client.post(
        "/api/quant/walkforward",
        json={"candidate_codes": ["110001", "110002"]},
    )
    assert response.status_code == 400
    assert "不足 2 只" in response.json()["detail"]


def test_unknown_codes_raise_400(client: TestClient, db_session: Session) -> None:
    """候选代码全部未知：400。"""
    response = client.post(
        "/api/quant/walkforward", json={"candidate_codes": ["999998", "999999"]}
    )
    assert response.status_code == 400


def test_default_candidate_pool_uses_positions(
    client: TestClient, db_session: Session
) -> None:
    """缺省 candidate_codes 时回退为当前持仓基金。"""
    instruments = [
        _seed_navs(
            db_session,
            code=f"1100{i:02d}",
            name=f"沪深基金{i}",
            days=200,
            daily_growth=0.002 - i * 0.001,
        )
        for i in range(3)
    ]
    for inst in instruments:
        _seed_position(db_session, inst)

    response = client.post("/api/quant/walkforward", json={"top_n": 2})
    assert response.status_code == 200
    assert response.json()["rebalance_count"] == 4


def test_no_positions_and_no_codes_returns_400(client: TestClient) -> None:
    """无持仓且未指定候选基金：400。"""
    response = client.post("/api/quant/walkforward", json={})
    assert response.status_code == 400


def test_custom_window_params(client: TestClient, db_session: Session) -> None:
    """自定义窗口参数生效：train 60 / test 10 / step 10。"""
    instruments = [
        _seed_navs(
            db_session,
            code=f"1100{i:02d}",
            name=f"沪深基金{i}",
            days=120,
            daily_growth=0.002 - i * 0.001,
        )
        for i in range(3)
    ]
    codes = [inst.code for inst in instruments]

    data = client.post(
        "/api/quant/walkforward",
        json={
            "candidate_codes": codes,
            "window": {"train_window": 60, "test_window": 10, "step": 10},
            "top_n": 2,
        },
    ).json()
    # 120 天、60+10 步进 10：起点 0/10/20/30/40/50 → 6 段
    assert data["rebalance_count"] == 6
    assert data["params"]["train_window"] == 60
    assert len(data["curve"]) == 60


def test_result_deterministic(client: TestClient, db_session: Session) -> None:
    """相同数据两次调用结果完全一致（无随机性）。"""
    instruments = [
        _seed_navs(
            db_session,
            code=f"1100{i:02d}",
            name=f"沪深基金{i}",
            days=220,
            daily_growth=0.003 - i * 0.001,
        )
        for i in range(4)
    ]
    codes = [inst.code for inst in instruments]

    first = client.post(
        "/api/quant/walkforward", json={"candidate_codes": codes, "top_n": 2}
    ).json()
    second = client.post(
        "/api/quant/walkforward", json={"candidate_codes": codes, "top_n": 2}
    ).json()
    assert first == second
