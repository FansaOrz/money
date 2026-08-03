"""A股多因子研究策略测试：因子引擎 / universe 过滤 / 组合构建 / 回测 / 仓储。

设计原则：
- 因子与回测引擎为纯函数，全部用内存构造的行情/财务数据驱动，不访问数据库；
- 仓储层用 MockRepository（duck-typed，与 StockRepository 协议一致）注入；
- 路由层经 FastAPI 依赖覆盖注入同一个 mock 仓储，不触碰生产数据。

重点验证：
1. 行业内 winsorize+z-score：极值被截断、行业间互不影响、方向调整（负债率取负）；
2. 12-1 动量跳过最近 21 日、趋势/低波动窗口语义；
3. PIT：available_at > 打分日的财务快照不可见；
4. universe：ST/停牌/次新（120 交易日）/流动性/历史样本过滤；
5. 组合构建：行业中性（只数份额 + 行业上限）、单股上限截断、现金兜底；
6. 回测：T 日信号 T+1 成交、涨跌停/停牌不可成交且顺延、费用口径
   （佣金最低 5 元、卖出印花税、双边滑点）、逐日盯市无重复累计；
7. 无未来数据：篡改信号日之后的行情不改变任何一期持仓；
8. validation：Rank IC / 五档单调性统计。
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient

from app.services import stock_backtest as backtest
from app.services import stock_factors as factors
from app.services import stock_strategy as strategy
from app.services.stock_repository import (
    Fundamentals,
    NamePeriod,
    StockBar,
    StockInfo,
    TradeCalendar,
    board_of,
    one_word_limit,
    price_limit_for,
    st_status_as_of,
    statutory_disclosure_deadline,
)

START = date(2025, 1, 1)


# ---------------------------------------------------------------------------
# 数据构造辅助
# ---------------------------------------------------------------------------


def _make_bars(
    code: str,
    days: int,
    growth: float = 0.001,
    start_close: float = 10.0,
    amount: float = 1e8,
    open_same_as_close: bool = True,
) -> list[StockBar]:
    """生成 days 根连续交易日的日线（几何增长 + 微小交替噪声）。"""
    bars: list[StockBar] = []
    close = start_close
    for i in range(days):
        noise = 0.0005 if i % 2 == 0 else -0.0005
        close *= 1 + growth + noise
        bars.append(
            StockBar(
                code=code,
                trade_date=START + timedelta(days=i),
                open=close if open_same_as_close else close * 0.998,
                high=close * 1.01,
                low=close * 0.99,
                close=close,
                volume=1e6,
                amount=amount,
                suspended=False,
            )
        )
    return bars


def _fundamentals(
    code: str,
    available: date = date(2024, 10, 1),
    roe: float = 0.12,
    gross_margin: float = 0.35,
    ocf_to_profit: float = 1.1,
    debt_ratio: float = 0.45,
    ep: float = 0.05,
    bp: float = 0.6,
) -> Fundamentals:
    return Fundamentals(
        code=code,
        available_at=available,
        period=date(2024, 6, 30),
        roe=roe,
        gross_margin=gross_margin,
        ocf_to_profit=ocf_to_profit,
        debt_ratio=debt_ratio,
        ep=ep,
        bp=bp,
    )


def _info(code: str, industry: str = "银行", name: str | None = None) -> StockInfo:
    return StockInfo(code=code, name=name or f"股票{code}", industry=industry)


def _closes(days: int, growth: float = 0.001) -> list[float]:
    values = [10.0]
    for _ in range(days - 1):
        values.append(values[-1] * (1 + growth))
    return values


# ---------------------------------------------------------------------------
# 因子引擎：窗口语义
# ---------------------------------------------------------------------------


def test_momentum_12_1_skips_recent_month() -> None:
    """12-1 动量跳过最近 21 日：端点为 T-21 与 T-273，最近 21 日涨跌不影响。"""
    base = _closes(300, 0.001)
    m1 = factors.momentum_12_1(base)
    assert m1 is not None
    expected = base[-22] / base[-274] - 1.0
    assert m1 == pytest.approx(expected, rel=1e-12)

    # 篡改最近 21 日（尾部 21 个点），动量不变
    tampered = base[:-21] + [v * 5.0 for v in base[-21:]]
    assert factors.momentum_12_1(tampered) == pytest.approx(m1, rel=1e-12)

    # 样本不足（< 274）返回 None
    assert factors.momentum_12_1(_closes(200)) is None


def test_trend_and_lowvol_windows() -> None:
    """趋势：多头排列 = +1；低波动：恒定序列波动为 0 → 因子为 0（最好）。"""
    up = _closes(100, 0.002)
    assert factors.trend_strength(up) == pytest.approx(1.0)
    down = _closes(100, -0.002)
    assert factors.trend_strength(down) == pytest.approx(-1.0)
    assert factors.trend_strength(_closes(10)) is None

    flat = [10.0] * 80
    assert factors.low_volatility(flat) == pytest.approx(0.0)
    volatile = [10.0 * (1.05 if i % 2 == 0 else 0.95) for i in range(80)]
    assert factors.low_volatility(volatile) < 0.0


def test_winsorize_and_zscore() -> None:
    """winsorize 截断极值；z-score 总体口径；常数列/小样本记 0。"""
    values = {f"s{i}": float(i) for i in range(100)}
    values["outlier"] = 1e6
    clipped = factors.winsorize(values)
    assert clipped["outlier"] is not None and clipped["outlier"] < 1e6
    # 1%/99% 分位截断：最大值被裁到 99% 分位
    assert clipped["outlier"] == pytest.approx(
        max(v for k, v in clipped.items() if k != "outlier"), rel=1e-2
    )

    z = factors.zscore({"a": 1.0, "b": 2.0, "c": 3.0})
    assert z["b"] == pytest.approx(0.0)
    # 总体标准差口径：{1,2,3} 的 σ = sqrt(2/3)，z = ±1/σ ≈ ±1.2247
    assert z["a"] == pytest.approx(-1.224744871391589, rel=1e-9)
    assert z["c"] == pytest.approx(1.224744871391589, rel=1e-9)

    # 常数列：无区分度，全部记 0
    assert factors.zscore({"a": 5.0, "b": 5.0}) == {"a": 0.0, "b": 0.0}
    # 单一样本：记 0；缺失保持 None
    assert factors.zscore({"a": 5.0}) == {"a": 0.0}
    assert factors.zscore({"a": None}) == {"a": None}


def test_industry_neutral_zscore() -> None:
    """行业中性：同行业内部 z-score，行业间互不影响。

    两个行业各 3 只股票；银行业 ROE 普遍高于公用事业，但行业内 z 值
    只反映行业内部相对位置 —— 银行业最低 ROE 的股票 z 仍为负。
    """
    contexts = []
    for i, (code, roe) in enumerate(
        [("b1", 0.20), ("b2", 0.15), ("b3", 0.10), ("u1", 0.06), ("u2", 0.05), ("u3", 0.04)]
    ):
        industry = "银行" if code.startswith("b") else "公用"
        info = StockInfo(code=code, name=code, industry=industry)
        ctx = factors.StockContext(
            info=info,
            bars=tuple(_make_bars(code, 280, 0.0005)),
            fundamentals=(_fundamentals(code, roe=roe),),
        )
        contexts.append(ctx)
    results = {r.code: r for r in factors.compute_cross_section(contexts, START + timedelta(days=300))}
    # 银行业最低 ROE（b3=0.10，仍高于公用全部）在行业内 z 为负
    assert results["b3"].zscores["roe"] is not None and results["b3"].zscores["roe"] < 0
    # 公用事业最高 ROE（u1=0.06）在行业内 z 为正
    assert results["u1"].zscores["roe"] is not None and results["u1"].zscores["roe"] > 0
    # 同行业 z 值之和 ≈ 0（总体均值中心化）
    bank_sum = sum(results[c].zscores["roe"] or 0 for c in ("b1", "b2", "b3"))
    assert bank_sum == pytest.approx(0.0, abs=1e-9)


def test_debt_ratio_direction_inverted() -> None:
    """负债率取负：负债越低的股票 quality 得分越高。"""
    contexts = []
    for code, debt in [("x1", 0.2), ("x2", 0.5), ("x3", 0.8)]:
        info = StockInfo(code=code, name=code, industry="制造")
        ctx = factors.StockContext(
            info=info,
            bars=tuple(_make_bars(code, 280, 0.0005)),
            fundamentals=(_fundamentals(code, debt_ratio=debt),),
        )
        contexts.append(ctx)
    results = {
        r.code: r
        for r in factors.compute_cross_section(contexts, START + timedelta(days=300))
    }
    assert results["x1"].zscores["debt_ratio"] > results["x3"].zscores["debt_ratio"]


def test_pit_fundamentals_not_visible_before_available() -> None:
    """PIT：打分日早于 available_at 的财务快照不可见（无未来数据）。"""
    info = _info("600001")
    bars = _make_bars("600001", 280, 0.0005)
    future_snap = _fundamentals("600001", available=date(2025, 6, 1), roe=0.30)
    as_of = date(2025, 5, 1)
    ctx = factors.build_context(info, bars, [future_snap], as_of)
    raw = factors.raw_factors(ctx, as_of)
    # 快照 2025-06-01 才可用，2025-05-01 打分时基本面全部缺失
    assert raw["roe"] is None
    assert raw["ep"] is None
    # 价格因子不受影响
    assert raw["trend"] is not None


# ---------------------------------------------------------------------------
# universe 过滤
# ---------------------------------------------------------------------------


def test_universe_st_and_suspension_and_new_listing() -> None:
    """ST 名称 / T 日停牌 / 上市未满 120 交易日，各自剔除且原因可解释。"""
    as_of = START + timedelta(days=300)
    bars = _make_bars("600001", 301)

    st = strategy.filter_universe_stock(_info("600001", name="ST股票"), bars, as_of)
    assert not st.passed and any("ST" in r for r in st.reasons)

    suspended_bars = list(bars)
    last = suspended_bars[-1]
    suspended_bars[-1] = StockBar(
        code=last.code, trade_date=last.trade_date, open=None, high=None,
        low=None, close=last.close, volume=0.0, amount=0.0, suspended=True,
    )
    suspended = strategy.filter_universe_stock(_info("600001"), suspended_bars, as_of)
    assert not suspended.passed and any("停牌" in r for r in suspended.reasons)

    new_stock = strategy.filter_universe_stock(
        _info("600002"), _make_bars("600002", 100), START + timedelta(days=99)
    )
    assert not new_stock.passed and any("120" in r for r in new_stock.reasons)


def test_universe_liquidity_filter() -> None:
    """近 20 日日均成交额低于阈值剔除；阈值以下/以上边界正确。"""
    as_of = START + timedelta(days=300)
    rich = strategy.filter_universe_stock(
        _info("600001"), _make_bars("600001", 301, amount=1e8), as_of
    )
    assert rich.passed
    poor = strategy.filter_universe_stock(
        _info("600001"), _make_bars("600001", 301, amount=1e6), as_of
    )
    assert not poor.passed and any("成交额" in r for r in poor.reasons)


# ---------------------------------------------------------------------------
# 组合构建：行业中性 + 上限
# ---------------------------------------------------------------------------

INDUSTRY_POOL = {
    "银行": ["600001", "600002", "600003"],
    "公用": ["000001", "000002", "000003"],
}


def _scored_pool() -> tuple[list[factors.FactorResult], list[StockInfo]]:
    """构造两行业六只股票的复合分结果（银行略强）。"""
    infos = [_info(code, industry) for industry, codes in INDUSTRY_POOL.items() for code in codes]
    as_of = START + timedelta(days=300)
    contexts = []
    for i, info in enumerate(infos):
        growth = 0.0008 - 0.0001 * i  # 复合分随下标递减
        contexts.append(
            factors.StockContext(
                info=info,
                bars=tuple(_make_bars(info.code, 301, growth)),
                fundamentals=(_fundamentals(info.code, roe=0.15 - 0.01 * i),),
            )
        )
    return factors.compute_cross_section(contexts, as_of), infos


def test_portfolio_industry_neutral_and_caps() -> None:
    """行业中性：两行业各 50% 份额（只数口径）；单股 ≤5% 截断；合计 ≤ 100%。"""
    scored, infos = _scored_pool()
    plan = strategy.build_portfolio(
        scored, infos, START + timedelta(days=300), top_n=6
    )
    assert plan.target_weights
    assert all(w <= 0.05 + 1e-9 for w in plan.target_weights.values())
    assert plan.invested_weight <= 1.0 + 1e-9
    # 两行业只数相同 → 目标份额各 50%，但 3 只 × 5% = 15% < 50% →
    # 每行业实际 15%，其余 70% 留现金（行业中性不跨行业倒灌）
    assert plan.industries.get("银行") == pytest.approx(0.15, abs=1e-6)
    assert plan.industries.get("公用") == pytest.approx(0.15, abs=1e-6)
    assert plan.invested_weight == pytest.approx(0.30, abs=1e-6)
    assert any("现金" in w for w in plan.warnings)


def test_portfolio_top_n_and_ranking() -> None:
    """top_n 截断：只有复合分最高的前 N 只入选。

    行业中性口径下各行业头部股票的复合分可比（行业内 z-score），
    因此 top_n=2 的入选集合 = 两行业各自的头部股票，且与全局排名一致。
    """
    scored, infos = _scored_pool()
    ranked = sorted(scored, key=lambda item: item.composite, reverse=True)
    plan = strategy.build_portfolio(
        scored, infos, START + timedelta(days=300), top_n=2
    )
    assert len(plan.target_weights) == 2
    assert set(plan.target_weights) == {ranked[0].code, ranked[1].code}
    # 两行业各占一席（行业内 z 使行业间可比，头部股得分接近）
    industries = {
        next(info.industry for info in infos if info.code == code)
        for code in plan.target_weights
    }
    assert industries == {"银行", "公用"}


# ---------------------------------------------------------------------------
# 回测：成交规则与费用
# ---------------------------------------------------------------------------


def test_price_limit_blocks_and_defers() -> None:
    """涨停不可买：订单顺延到下一交易日成交（首期调仓明细断言）。

    回测起点取第 300 根 bar（universe 的上市天数/历史样本均已满足）；
    首期信号日为起点（非月末强制建仓），T+1（下标 301）构造涨停
    （raw_return +9.9%），其后交易日恢复正常。
    """
    code = "600001"
    bars = _make_bars(code, 320, 0.0005)
    t1 = bars[301]
    bars[301] = StockBar(
        code=code, trade_date=t1.trade_date, open=t1.open, high=t1.high,
        low=t1.low, close=t1.close, volume=1e6, amount=1e8,
        suspended=False, raw_return=0.099,
    )
    calendar_days = [bar.trade_date for bar in bars[300:308]]
    panel = backtest.MarketPanel(
        calendar=TradeCalendar(tuple(calendar_days)),
        bars_by_code={code: bars},
        bar_lookup={code: {bar.trade_date: bar for bar in bars}},
        index_series=[],
    )
    config = backtest.BacktestConfig(
        start=calendar_days[0],
        end=calendar_days[-1],
        initial_capital=1_000_000.0,
        top_n=1,
        max_stock_weight=0.05,
    )
    outcome = backtest.run_backtest_panel(
        panel, [_info(code)], {code: [_fundamentals(code)]}, config
    )
    detail = outcome.rebalances[0]
    assert detail.target  # 首期确有目标持仓
    # 首笔买入不在涨停的 T+1（顺延），且不晚于区间末日
    first_buy = next(fill for fill in detail.fills if fill.action == "buy")
    assert first_buy.fill_date > bars[301].trade_date
    assert code in detail.blocked_codes


def test_fee_model_min_commission_and_stamp_tax() -> None:
    """费用口径：佣金双边最低 5 元，印花税仅卖出，滑点双边。"""
    cost = backtest.CostModel(
        commission_rate=0.00025, min_commission=5.0,
        stamp_tax_rate=0.0005, slippage_rate=0.001,
    )
    # 小额买入：0.00025×10000=2.5 < 5 → 最低 5 元，无印花税
    assert backtest.trade_fee("buy", 10_000.0, cost) == pytest.approx(5.0)
    # 大额卖出：佣金 25 + 印花税 50
    assert backtest.trade_fee("sell", 100_000.0, cost) == pytest.approx(75.0)

    bar = StockBar(
        code="600001", trade_date=START, open=10.0, high=10.1,
        low=9.9, close=10.0, volume=1e6, amount=1e7,
    )
    assert backtest.trade_price(bar, "buy", 0.001) == pytest.approx(10.01)
    assert backtest.trade_price(bar, "sell", 0.001) == pytest.approx(9.99)


def test_suspension_blocks_trade() -> None:
    """停牌不可成交（买卖双向）。"""
    suspended_bar = StockBar(
        code="600001", trade_date=START, open=None, high=None, low=None,
        close=10.0, volume=0.0, amount=0.0, suspended=True,
    )
    ok, reason = backtest.can_trade(suspended_bar, 10.0, "buy", 0.098)
    assert not ok and "停牌" in reason
    ok, _ = backtest.can_trade(None, 10.0, "sell", 0.098)
    assert not ok


def test_backtest_flat_market_deterministic() -> None:
    """横盘市场：组合净值 ≈ 初始资金减费用；等权基准恒 1；结果确定可复现。"""
    codes = ["600001", "600002"]
    days = 320
    panels = {code: [10.0 + 0.01 * (i % 2) for i in range(days)] for code in codes}
    calendar_days = [START + timedelta(days=i) for i in range(days)]
    bars_by_code = {
        code: [
            StockBar(
                code=code, trade_date=calendar_days[i], open=panels[code][i],
                high=panels[code][i], low=panels[code][i], close=panels[code][i],
                volume=1e6, amount=1e8,
            )
            for i in range(days)
        ]
        for code in codes
    }
    panel = backtest.MarketPanel(
        calendar=TradeCalendar(tuple(calendar_days)),
        bars_by_code=bars_by_code,
        bar_lookup={
            code: {bar.trade_date: bar for bar in bars_by_code[code]} for code in codes
        },
        index_series=[],
    )
    config = backtest.BacktestConfig(
        start=calendar_days[200], end=calendar_days[-1],
        initial_capital=1_000_000.0, top_n=2,
    )
    infos = [_info(code) for code in codes]
    fundamentals_by_code = {code: [_fundamentals(code)] for code in codes}
    outcome1 = backtest.run_backtest_panel(panel, infos, fundamentals_by_code, config)
    outcome2 = backtest.run_backtest_panel(panel, infos, fundamentals_by_code, config)
    assert outcome1.equity == outcome2.equity  # 确定性
    assert outcome1.benchmark_kind == "equal_weight"
    # 横盘：买入后市值在初始资金的 ±1% 以内（费用 + 微小波动）
    assert outcome1.final_value == pytest.approx(1_000_000.0, rel=0.01)
    assert outcome1.total_fees > 0  # 真实发生了调仓与费用
    assert len(outcome1.equity) == len(outcome1.benchmark) == len(outcome1.calendar)


def test_backtest_no_lookahead() -> None:
    """无未来数据：篡改信号日之后的行情，各期目标持仓完全不变。"""
    codes = ["600001", "600002", "600003"]
    days = 320
    calendar_days = [START + timedelta(days=i) for i in range(days)]

    def _bars(growth: float) -> dict[str, list[StockBar]]:
        return {
            code: _make_bars(code, days, growth + 0.0001 * i, amount=1e8)
            for i, code in enumerate(codes)
        }

    infos = [_info(code) for code in codes]
    fundamentals_by_code = {
        code: [_fundamentals(code, roe=0.15 - 0.01 * i)]
        for i, code in enumerate(codes)
    }
    config = backtest.BacktestConfig(
        start=calendar_days[0], end=calendar_days[-1],
        initial_capital=1_000_000.0, top_n=2,
    )

    def _targets(bars_by_code: dict[str, list[StockBar]]) -> list[dict[str, float]]:
        panel = backtest.MarketPanel(
            calendar=TradeCalendar(tuple(calendar_days)),
            bars_by_code=bars_by_code,
            bar_lookup={
                code: {bar.trade_date: bar for bar in bars_by_code[code]}
                for code in codes
            },
            index_series=[],
        )
        outcome = backtest.run_backtest_panel(
            panel, infos, fundamentals_by_code, config
        )
        return [detail.target for detail in outcome.rebalances]

    base_targets = _targets(_bars(0.0006))
    last_signal = max(
        detail_day
        for detail_day in strategy.month_ends(calendar_days)
        if detail_day < calendar_days[-1]
    )
    tampered = _bars(0.0006)
    for code in codes:
        tampered[code] = [
            StockBar(
                code=bar.code, trade_date=bar.trade_date, open=bar.open,
                high=bar.high, low=bar.low,
                close=bar.close * 3.0 if bar.trade_date > last_signal else bar.close,
                volume=bar.volume, amount=bar.amount, suspended=bar.suspended,
            )
            for bar in tampered[code]
        ]
    assert _targets(tampered) == base_targets


def test_validation_stats_rank_ic() -> None:
    """validation：完美正相关时 Rank IC = 1；五档单调性方向正确。

    scores_by_date 的最后一期按约定没有前瞻收益（引擎语义），
    因此需要两个信号日才能让第一期进入统计。
    """
    d1, d2 = date(2025, 1, 31), date(2025, 2, 28)
    scores = [
        (d1, {"a": 3.0, "b": 2.0, "c": 1.0, "d": 0.5, "e": 0.1, "f": -1.0}),
        (d2, {"a": 3.0, "b": 2.0, "c": 1.0, "d": 0.5, "e": 0.1, "f": -1.0}),
    ]
    forwards = [(d1, {"a": 0.30, "b": 0.20, "c": 0.10, "d": 0.05, "e": 0.01, "f": -0.10})]
    result = backtest.validation_stats(scores, forwards)
    assert result["rank_ic_mean"] == pytest.approx(1.0)
    assert result["rank_ic_count"] == 1
    # 五档单调性需要 ≥10 个样本（quant_stats 口径），6 只股票时不产出
    assert result["quintile_returns"] == []
    assert result["quintile_spread"] is None

    # 样本充足（两个有前瞻收益的信号日 × 6 只 = 12）时：五档 spread 为正
    d3 = date(2025, 3, 31)
    scores3 = scores + [(d3, {"a": 3.0, "b": 2.0, "c": 1.0, "d": 0.5, "e": 0.1, "f": -1.0})]
    forwards2 = [
        (d1, {"a": 0.30, "b": 0.20, "c": 0.10, "d": 0.05, "e": 0.01, "f": -0.10}),
        (d2, {"a": 0.28, "b": 0.18, "c": 0.09, "d": 0.04, "e": 0.02, "f": -0.08}),
    ]
    result2 = backtest.validation_stats(scores3, forwards2)
    assert result2["quintile_spread"] is not None and result2["quintile_spread"] > 0


# ---------------------------------------------------------------------------
# 仓储：mock 注入 + 动态装载
# ---------------------------------------------------------------------------


class MockRepository:
    """内存 mock 仓储（duck-typed StockRepository）。"""

    def __init__(
        self,
        infos: list[StockInfo],
        bars_by_code: dict[str, list[StockBar]],
        fundamentals_by_code: dict[str, list[Fundamentals]],
        index: list[tuple[date, float]] | None = None,
    ) -> None:
        self._infos = infos
        self._bars = bars_by_code
        self._fundamentals = fundamentals_by_code
        self._index = index or []

    def list_stocks(self, codes: list[str] | None = None) -> list[StockInfo]:
        if codes is None:
            return list(self._infos)
        wanted = set(codes)
        return [info for info in self._infos if info.code in wanted]

    def trade_calendar(self, start: date | None, end: date | None) -> TradeCalendar:
        days = sorted(
            {bar.trade_date for bars in self._bars.values() for bar in bars}
        )
        if start is not None:
            days = [d for d in days if d >= start]
        if end is not None:
            days = [d for d in days if d <= end]
        return TradeCalendar(tuple(days))

    def daily_bars(
        self,
        codes: list[str] | None = None,
        start: date | None = None,
        end: date | None = None,
    ) -> list[StockBar]:
        wanted = set(codes) if codes else set(self._bars)
        result: list[StockBar] = []
        for code in wanted:
            for bar in self._bars.get(code, []):
                if start is not None and bar.trade_date < start:
                    continue
                if end is not None and bar.trade_date > end:
                    continue
                result.append(bar)
        return result

    def fundamentals(
        self, codes: list[str] | None = None, as_of: date | None = None
    ) -> list[Fundamentals]:
        wanted = set(codes) if codes else set(self._fundamentals)
        result: list[Fundamentals] = []
        for code in wanted:
            for snap in self._fundamentals.get(code, []):
                if as_of is not None and snap.available_at > as_of:
                    continue
                result.append(snap)
        return result

    def index_bars(
        self, index_code: str, start: date | None = None, end: date | None = None
    ) -> list[tuple[date, float]]:
        return [
            (day, value)
            for day, value in self._index
            if (start is None or day >= start) and (end is None or day <= end)
        ]


def test_run_backtest_with_mock_repository() -> None:
    """编排层：注入 mock 仓储完成全链路回测（含基准与 validation）。"""
    codes = ["600001", "600002", "600003"]
    days = 320
    repo = MockRepository(
        infos=[_info(code) for code in codes],
        bars_by_code={
            code: _make_bars(code, days, 0.0005 + 0.0001 * i, amount=1e8)
            for i, code in enumerate(codes)
        },
        fundamentals_by_code={
            code: [_fundamentals(code, roe=0.15 - 0.01 * i)]
            for i, code in enumerate(codes)
        },
        index=[(START + timedelta(days=i), 4000.0 * (1.0003**i)) for i in range(days)],
    )
    config = backtest.BacktestConfig(
        start=START, end=START + timedelta(days=days - 1),
        initial_capital=1_000_000.0, top_n=2, benchmark_index="CSI300",
    )
    outcome = backtest.run_backtest(config=config, repository=repo)
    assert outcome.benchmark_kind == "index:CSI300"
    assert outcome.final_value > 0
    assert outcome.rebalances
    assert len(outcome.equity) == len(outcome.calendar) == len(outcome.benchmark)
    # 等权/指数基准与策略逐日对齐
    assert outcome.benchmark[0] == pytest.approx(1.0)


def test_run_backtest_no_repository() -> None:
    """仓储全部不可用（db=None、未注入）：BacktestError 明确提示。"""
    with pytest.raises(backtest.BacktestError, match="仓储不可用"):
        backtest.run_backtest(
            db=None,
            config=backtest.BacktestConfig(
                start=date(2025, 1, 1), end=date(2025, 12, 31)
            ),
        )


# ---------------------------------------------------------------------------
# A股规则链回归：板块幅度 / 前收盘口径 / 一字板 / ST as_of / pending 覆盖
# ---------------------------------------------------------------------------


def test_board_and_price_limit_rules() -> None:
    """板块识别与涨跌停幅度：主板 10%、30/68 为 20%、北交所 4/8/92 为 30%、ST 5%。"""
    assert board_of("600519") == "main"
    assert board_of("000001") == "main"
    assert board_of("300750") == "chinext"
    assert board_of("688981") == "star"
    assert board_of("430047") == "bse"
    assert board_of("830799") == "bse"
    assert board_of("920001") == "bse"  # 北交所 92 新号段

    assert price_limit_for("600519") == pytest.approx(0.10)
    assert price_limit_for("300750") == pytest.approx(0.20)
    assert price_limit_for("688981") == pytest.approx(0.20)
    assert price_limit_for("430047") == pytest.approx(0.30)
    assert price_limit_for("920001") == pytest.approx(0.30)
    assert price_limit_for("600519", st=True) == pytest.approx(0.05)
    # 北交所无 ST 5% 档：恒 30%
    assert price_limit_for("830799", st=True) == pytest.approx(0.30)


def test_one_word_limit_detection() -> None:
    """一字板：振幅≈0 且触板（法定板幅 ±ε 带内）；有振幅或非触板不算。"""
    flat = StockBar(
        code="600001", trade_date=START, open=11.0, high=11.0, low=11.0,
        close=11.0, volume=1e6, amount=1e7,
    )
    # limit 参数为法定板幅（主板 10%）：+10.0% 触板 + 零振幅 → 一字
    assert one_word_limit(flat, 0.10, 0.10)
    assert one_word_limit(flat, 0.0999, 0.10)  # 容忍带（±2‰×板幅）内仍视为一字
    ranged = StockBar(
        code="600001", trade_date=START, open=10.5, high=11.0, low=10.2,
        close=11.0, volume=1e6, amount=1e7,
    )
    assert not one_word_limit(ranged, 0.10, 0.10)  # 有振幅：非一字
    assert not one_word_limit(flat, 0.05, 0.10)  # 零振幅但未触板
    nohl = StockBar(
        code="600001", trade_date=START, open=None, high=None, low=None,
        close=11.0, volume=1e6, amount=1e7,
    )
    assert not one_word_limit(nohl, 0.10, 0.10)  # high/low 缺失保守放行


def test_prev_bar_before_strictly_earlier() -> None:
    """前收盘必须严格早于成交日：同日 bar 不当前收盘（修复涨跌幅稀释）。"""
    bars = _make_bars("600001", 5)
    day = bars[2].trade_date
    prev = backtest.prev_bar_before(bars, day)
    assert prev is not None and prev.trade_date == bars[1].trade_date
    # 首日无严格更早的 bar
    assert backtest.prev_bar_before(bars, bars[0].trade_date) is None


def test_limit_up_blocked_with_suspension_gap() -> None:
    """停牌期 close 填前收盘的数据形态下，涨停判定不失效（Critical 回归）。

    构造：T-1 停牌（close 沿用前收盘），T 日复牌涨停（close = 真实前收
    ×1.10）。若前收盘取「≤T」会取到停牌日的填充价 10.0（=T-2 收盘），
    涨跌幅仍 +10% —— 该形态旧口径尚可；真正的失效形态是数据源在复牌日
    也把 close 回填为涨停价后，「≤T」口径把当日 close 当自己前收盘。
    这里直接验证：prev 取 T-1（填充价 10.0），move = +10% → 阻塞。
    """
    code = "600001"
    bars = _make_bars(code, 3, growth=0.0, start_close=10.0)
    suspended = StockBar(
        code=code, trade_date=bars[1].trade_date, open=None, high=None,
        low=None, close=bars[0].close, volume=0.0, amount=0.0, suspended=True,
    )
    limit_up = StockBar(
        code=code, trade_date=bars[2].trade_date, open=11.0, high=11.0,
        low=10.9, close=bars[0].close * 1.10, volume=1e6, amount=1e8,
    )
    series = [bars[0], suspended, limit_up]
    prev = backtest.prev_bar_before(series, limit_up.trade_date)
    assert prev is not None and prev.trade_date == suspended.trade_date
    ok, reason = backtest.can_trade(limit_up, prev.close, "buy", 0.098)
    assert not ok and "涨停" in reason
    # 卖出方向不受涨停限制
    ok_sell, _ = backtest.can_trade(limit_up, prev.close, "sell", 0.098)
    assert ok_sell


def test_can_trade_per_board_limits() -> None:
    """分板块触发线：同一 +15% 涨幅，主板/ST 阻塞买入，创业/科创/北交所放行。"""
    bar = StockBar(
        code="000002", trade_date=START, open=11.5, high=11.6, low=11.2,
        close=11.5, volume=1e6, amount=1e8,
    )
    prev_close = 10.0  # move = +15%
    ok_main, _ = backtest.can_trade(bar, prev_close, "buy", 0.98, code="600001")
    assert not ok_main  # 主板触发线 10%×0.98=9.8% < 15%
    ok_st, reason = backtest.can_trade(bar, prev_close, "buy", 0.98, code="600001", st=True)
    assert not ok_st and "涨停" in reason  # ST 触发线 5%×0.98=4.9%
    ok_gem, _ = backtest.can_trade(bar, prev_close, "buy", 0.98, code="300750")
    assert ok_gem  # 创业板触发线 20%×0.98=19.6% > 15%
    ok_star, _ = backtest.can_trade(bar, prev_close, "buy", 0.98, code="688981")
    assert ok_star
    ok_bse, _ = backtest.can_trade(bar, prev_close, "buy", 0.98, code="920001")
    assert ok_bse  # 北交所触发线 30%×0.98=29.4%


def test_one_word_limit_blocks_both_sides() -> None:
    """一字涨停：买卖双向均不可成交；一字跌停同样双向阻塞。"""
    one_word_up = StockBar(
        code="600001", trade_date=START, open=11.0, high=11.0, low=11.0,
        close=11.0, volume=1e6, amount=1e7,
    )
    prev_close = 10.0  # move = +10%，主板法定板幅 10% 触板
    ok_buy, reason_buy = backtest.can_trade(one_word_up, prev_close, "buy", 0.98)
    assert not ok_buy and "一字" in reason_buy
    ok_sell, reason_sell = backtest.can_trade(one_word_up, prev_close, "sell", 0.98)
    assert not ok_sell and "一字" in reason_sell

    one_word_down = StockBar(
        code="600001", trade_date=START, open=9.0, high=9.0, low=9.0,
        close=9.0, volume=1e6, amount=1e7,
    )
    ok_sell_down, reason_down = backtest.can_trade(one_word_down, 10.0, "sell", 0.98)
    assert not ok_sell_down and "一字" in reason_down
    ok_buy_down, _ = backtest.can_trade(one_word_down, 10.0, "buy", 0.98)
    assert not ok_buy_down


def test_st_status_as_of_name_history() -> None:
    """历史 ST 判定：名称区间命中按区间 is_st；无覆盖回退当前名称。"""
    periods = [
        NamePeriod(code="600001", name="普通股份", start_date=date(2020, 1, 1),
                   end_date=date(2021, 6, 30), is_st=False),
        NamePeriod(code="600001", name="ST股份", start_date=date(2021, 7, 1),
                   end_date=date(2022, 5, 31), is_st=True),
    ]
    # 命中 ST 区间 → True（即使当前名称非 ST）
    assert st_status_as_of("普通股份", periods, date(2021, 12, 1))
    # 命中非 ST 区间 → False（即使当前名称是 ST —— 历史时点尚未戴帽）
    assert not st_status_as_of("ST股份", periods, date(2020, 6, 1))
    # 无区间覆盖（2023 年）→ 回退当前名称判定
    assert st_status_as_of("ST股份", periods, date(2023, 1, 1))
    assert not st_status_as_of("普通股份", periods, date(2023, 1, 1))
    # 无历史数据 → 当前名称
    assert st_status_as_of("*ST某某", None, date(2021, 1, 1))


def test_universe_uses_name_history_for_st() -> None:
    """universe 过滤按 as_of 当日名称剔除历史 ST（当前名称非 ST 也剔除）。"""
    as_of = date(2021, 12, 15)
    bars = _make_bars("600001", 320, 0.0005)
    # 当前名称已摘帽，但 as_of 落在 ST 区间内
    periods = [
        NamePeriod(code="600001", name="ST股份", start_date=date(2021, 7, 1),
                   end_date=date(2022, 5, 31), is_st=True),
    ]
    result = strategy.filter_universe_stock(
        _info("600001", name="普通股份"), bars, as_of, name_periods=periods
    )
    assert not result.passed and any("ST" in r for r in result.reasons)
    # as_of 在 ST 区间外 → 通过（其余条件已满足）
    ok_day = date(2025, 10, 27)  # START(2025-01-01)+300 天附近，样本充足
    bars2 = _make_bars("600001", 301, 0.0005)
    result2 = strategy.filter_universe_stock(
        _info("600001", name="普通股份"), bars2, ok_day, name_periods=periods
    )
    assert result2.passed


def test_pending_orders_overridden_warns() -> None:
    """涨停顺延保住首期订单（不静默丢），且区间末未成交订单显式 warning。

    首期信号日（1-31）目标 5%；2-25/2-26 涨停顺延，2-27 成交（首期不丢）；
    第二期信号日（2-28）为区间最后一交易日，其订单顺延至区间外 →
    outcome.warnings 显式记录区间末未成交。
    """
    code = "600001"
    history_days = [date(2024, 5, 16) + timedelta(days=i) for i in range(260)]
    jan_days = [date(2025, 1, 13) + timedelta(days=i) for i in range(19)]  # 1-13~1-31
    feb_days = [date(2025, 2, 3) + timedelta(days=i) for i in range(26)]  # 2-3~2-28
    all_days = history_days + jan_days + feb_days

    bars: list[StockBar] = []
    close = 10.0
    for day in all_days:
        close *= 1.0005
        bars.append(
            StockBar(
                code=code, trade_date=day, open=close, high=close * 1.01,
                low=close * 0.99, close=close, volume=1e6, amount=1e8,
            )
        )
    signal1 = date(2025, 1, 31)
    signal2 = date(2025, 2, 28)
    assert strategy.month_ends([d for d in all_days if d >= signal1]) == [signal1, signal2]
    # 首期 T+1（2-25 前的 2-3..2-24 正常成交）——为构造顺延，把 2-3 设为涨停
    for day in (date(2025, 2, 3), date(2025, 2, 4)):
        idx = all_days.index(day)
        bar = bars[idx]
        bars[idx] = StockBar(
            code=code, trade_date=day, open=bar.open, high=bar.high,
            low=bar.low, close=bar.close, volume=1e6, amount=1e8,
            suspended=False, raw_return=0.099,
        )

    calendar_days = [d for d in all_days if signal1 <= d <= signal2]
    panel = backtest.MarketPanel(
        calendar=TradeCalendar(tuple(calendar_days)),
        bars_by_code={code: bars},
        bar_lookup={code: {bar.trade_date: bar for bar in bars}},
        index_series=[],
    )
    config = backtest.BacktestConfig(
        start=signal1, end=signal2,
        initial_capital=1_000_000.0, top_n=1, max_stock_weight=0.05,
    )
    outcome = backtest.run_backtest_panel(
        panel, [_info(code)], {code: [_fundamentals(code)]}, config
    )
    assert len(outcome.rebalances) == 2
    first, second = outcome.rebalances
    assert first.target.get(code) == pytest.approx(0.05)
    # 2-3/2-4 涨停顺延，首期买入最终在 2-5 成交（顺延保住首期，不静默丢）
    assert code in first.blocked_codes
    buys = [fill for fill in first.fills if fill.action == "buy"]
    assert buys and buys[0].fill_date == date(2025, 2, 5)
    # 第二期信号日为区间最后一交易日：订单顺延至区间外 → 显式 warning
    assert any("未成交" in w for w in outcome.warnings)
    assert any("未成交" in w for w in second.warnings)


def test_pending_orders_dropped_by_new_signal_warns() -> None:
    """新信号覆盖未执行订单：显式 warning（覆盖路径，不静默丢单）。

    首期信号日（起点 10-25）目标 5%；执行日（10-26 起至次信号日前）
    全部周末无行情顺延；1 月末信号日（10-31）覆盖首期订单 →
    outcome.warnings 与被覆盖期 rebalance detail 均记录覆盖提示。
    """
    code = "600001"
    bars = _make_bars(code, 320, 0.0005)  # bar 日期为 2025-01-01 起连续 320 天
    jan = [date(2025, 10, 25) + timedelta(days=i) for i in range(7)]  # 10-25~10-31
    feb = [date(2025, 11, 24) + timedelta(days=i) for i in range(7)]  # 11-24~11-30
    calendar_days = jan + feb
    assert strategy.month_ends(calendar_days) == [date(2025, 10, 31), date(2025, 11, 30)]
    # 首期（10-25 起点信号）与次期（10-31 月末信号）之间的全部执行日
    # （10-26~10-30）从 bar_lookup 剔除（模拟无行情顺延），首期订单在
    # 10-31 信号日被覆盖。
    lookup = {bar.trade_date: bar for bar in bars}
    for gap_day in [date(2025, 10, 26) + timedelta(days=i) for i in range(5)]:
        lookup.pop(gap_day, None)
    panel = backtest.MarketPanel(
        calendar=TradeCalendar(tuple(calendar_days)),
        bars_by_code={code: bars},
        bar_lookup={code: lookup},
        index_series=[],
    )
    config = backtest.BacktestConfig(
        start=calendar_days[0], end=calendar_days[-1],
        initial_capital=1_000_000.0, top_n=1, max_stock_weight=0.05,
    )
    outcome = backtest.run_backtest_panel(
        panel, [_info(code)], {code: [_fundamentals(code)]}, config
    )
    # 起点强制信号 + 两个月末信号：起点期订单顺延至 10-31 信号日被覆盖
    assert len(outcome.rebalances) == 3
    assert outcome.rebalances[0].target.get(code) == pytest.approx(0.05)
    assert any("覆盖" in w for w in outcome.warnings)
    assert any("覆盖" in w for w in outcome.rebalances[0].warnings)


def test_first_period_blocked_buy_eventually_fills() -> None:
    """首期订单不因覆盖丢失：涨停顺延后在下一交易日成交（首期不被静默丢弃）。"""
    code = "600001"
    bars = _make_bars(code, 320, 0.0005)
    t1 = bars[301]
    bars[301] = StockBar(
        code=code, trade_date=t1.trade_date, open=t1.open, high=t1.high,
        low=t1.low, close=t1.close, volume=1e6, amount=1e8,
        suspended=False, raw_return=0.099,
    )
    calendar_days = [bar.trade_date for bar in bars[300:308]]
    panel = backtest.MarketPanel(
        calendar=TradeCalendar(tuple(calendar_days)),
        bars_by_code={code: bars},
        bar_lookup={code: {bar.trade_date: bar for bar in bars}},
        index_series=[],
    )
    config = backtest.BacktestConfig(
        start=calendar_days[0], end=calendar_days[-1],
        initial_capital=1_000_000.0, top_n=1, max_stock_weight=0.05,
    )
    outcome = backtest.run_backtest_panel(
        panel, [_info(code)], {code: [_fundamentals(code)]}, config
    )
    detail = outcome.rebalances[0]
    buys = [fill for fill in detail.fills if fill.action == "buy"]
    assert buys, "首期买入订单必须在顺延期间成交"
    assert buys[0].fill_date == bars[302].trade_date  # T+2 成交（T+1 涨停顺延）


def test_turnover_detail_real_values() -> None:
    """换手率明细真实赋值：建仓 5% 单股 → 首期 turnover = 0.025（Σ|Δw|/2）。"""
    code = "600001"
    bars = _make_bars(code, 320, 0.0005)
    calendar_days = [bar.trade_date for bar in bars[300:308]]
    panel = backtest.MarketPanel(
        calendar=TradeCalendar(tuple(calendar_days)),
        bars_by_code={code: bars},
        bar_lookup={code: {bar.trade_date: bar for bar in bars}},
        index_series=[],
    )
    config = backtest.BacktestConfig(
        start=calendar_days[0], end=calendar_days[-1],
        initial_capital=1_000_000.0, top_n=1, max_stock_weight=0.05,
    )
    outcome = backtest.run_backtest_panel(
        panel, [_info(code)], {code: [_fundamentals(code)]}, config
    )
    detail = outcome.rebalances[0]
    assert detail.turnover == pytest.approx(0.025, abs=1e-4)
    assert outcome.avg_turnover > 0


def test_full_suspension_excluded_from_forward_returns() -> None:
    """全程停牌股不进前瞻收益：信号日/次日均无有效收盘 → 无 forward 记录。"""
    codes = ["600001", "600002"]
    calendar_days = [START + timedelta(days=i) for i in range(300, 320)]
    bars_ok = _make_bars("600001", 320, 0.0005)
    suspended_bars = [
        StockBar(
            code="600002", trade_date=bar.trade_date, open=None, high=None,
            low=None, close=10.0, volume=0.0, amount=0.0, suspended=True,
        )
        for bar in _make_bars("600002", 320, 0.0005)
    ]
    panel = backtest.MarketPanel(
        calendar=TradeCalendar(tuple(calendar_days)),
        bars_by_code={"600001": bars_ok, "600002": suspended_bars},
        bar_lookup={
            "600001": {bar.trade_date: bar for bar in bars_ok},
            "600002": {bar.trade_date: bar for bar in suspended_bars},
        },
        index_series=[],
    )
    infos = [_info("600001"), _info("600002")]
    fundamentals_by_code = {code: [_fundamentals(code)] for code in codes}
    config = backtest.BacktestConfig(
        start=calendar_days[0], end=calendar_days[-1],
        initial_capital=1_000_000.0, top_n=2,
    )
    outcome = backtest.run_backtest_panel(panel, infos, fundamentals_by_code, config)
    # 停牌股从未通过 universe，不会出现在任何打分/前瞻收益中
    for _day, forwards in outcome.forward_returns:
        assert "600002" not in forwards
    for _day, scores in outcome.scores_by_date:
        assert "600002" not in scores


def test_industry_coverage_gate() -> None:
    """行业全未知：ratio=0 < 门槛，build_portfolio 抛 IndustryCoverageError；
    显式降级模式仅记 warning。"""
    scored, _infos = _scored_pool()
    unknown_infos = [
        StockInfo(code=info.code, name=info.name, industry="未知") for info in _infos
    ]
    with pytest.raises(strategy.IndustryCoverageError, match="行业数据覆盖不足"):
        strategy.build_portfolio(
            scored, unknown_infos, START + timedelta(days=300), top_n=6
        )
    # 降级模式（研究性因子查询）：不抛，但 warning 明确
    plan = strategy.build_portfolio(
        scored, unknown_infos, START + timedelta(days=300), top_n=6,
        enforce_industry_coverage=False,
    )
    assert plan.industry_known_ratio == pytest.approx(0.0)
    assert any("行业数据覆盖不足" in w for w in plan.warnings)


def test_dual_layer_panel_research_qfq_exec_raw() -> None:
    """MarketPanel 双口径：research(qfq) 与 exec(raw) 分离；缺失 qfq 回退 raw。"""
    exec_bars = _make_bars("600001", 10, 0.001, start_close=10.0)
    qfq_bars = [
        StockBar(
            code=bar.code, trade_date=bar.trade_date, open=bar.open * 0.5,
            high=bar.high * 0.5, low=bar.low * 0.5, close=bar.close * 0.5,
            volume=bar.volume, amount=bar.amount, suspended=bar.suspended,
        )
        for bar in exec_bars
    ]
    panel = backtest.MarketPanel(
        calendar=TradeCalendar(tuple(bar.trade_date for bar in exec_bars)),
        bars_by_code={"600001": exec_bars, "600002": _make_bars("600002", 10, 0.001)},
        bar_lookup={},
        index_series=[],
        research_bars_by_code={"600001": qfq_bars},
    )
    research = panel.research_series("600001")
    assert research[0].close == pytest.approx(exec_bars[0].close * 0.5)
    # 无 qfq 的股票回退执行口径
    assert panel.research_series("600002") is panel.bars_by_code["600002"]


def test_statutory_disclosure_deadline() -> None:
    """法定最晚披露日：年报次年 4-30、半年报 8-31、季报 4-30/10-31。"""
    assert statutory_disclosure_deadline(date(2024, 12, 31)) == date(2025, 4, 30)
    assert statutory_disclosure_deadline(date(2024, 6, 30)) == date(2024, 8, 31)
    assert statutory_disclosure_deadline(date(2024, 3, 31)) == date(2024, 4, 30)
    assert statutory_disclosure_deadline(date(2024, 9, 30)) == date(2024, 10, 31)


# ---------------------------------------------------------------------------
# 路由层：mock repository 经依赖覆盖注入（不动生产数据库）
# ---------------------------------------------------------------------------


@pytest.fixture()
def mock_repo() -> MockRepository:
    return MockRepository(
        infos=[_info(code, industry) for industry, codes in INDUSTRY_POOL.items() for code in codes],
        bars_by_code={
            code: _make_bars(code, 320, growth, amount=1e8)
            for code, growth in (
                ("600001", 0.0008), ("600002", 0.0005), ("600003", 0.0006),
                ("000001", -0.0003), ("000002", 0.0004), ("000003", 0.0001),
            )
        },
        fundamentals_by_code={
            code: [_fundamentals(code, roe=roe)]
            for code, roe in (
                ("600001", 0.16), ("600002", 0.12), ("600003", 0.13),
                ("000001", 0.08), ("000002", 0.09), ("000003", 0.07),
            )
        },
    )


def test_api_factors_with_mock_repository(client: TestClient, mock_repo) -> None:
    """POST /api/stocks/research/factors：mock 仓储注入，横截面打分 200。"""
    from app.api.routes import stocks_research

    client.app.dependency_overrides[stocks_research.get_stock_repository] = (
        lambda: mock_repo
    )
    try:
        response = client.post(
            "/api/stocks/research/factors", json={"as_of": "2025-11-16"}
        )
    finally:
        client.app.dependency_overrides.pop(stocks_research.get_stock_repository, None)
    assert response.status_code == 200
    data = response.json()
    assert data["universe_count"] == 6
    assert len(data["rows"]) == 6
    assert data["rows"][0]["rank"] == 1
    assert {row["code"] for row in data["rows"]} == {
        "600001", "600002", "600003", "000001", "000002", "000003",
    }
    # 复合分降序且行业内 z 已计算
    composites = [row["composite"] for row in data["rows"]]
    assert composites == sorted(composites, reverse=True)
    assert "roe" in data["rows"][0]["zscores"]
    assert data["methodology"]


def test_api_backtest_with_mock_repository(client: TestClient, mock_repo) -> None:
    """POST /api/stocks/research/backtest：月调仓回测 200，汇总字段完整。"""
    from app.api.routes import stocks_research

    client.app.dependency_overrides[stocks_research.get_stock_repository] = (
        lambda: mock_repo
    )
    try:
        response = client.post(
            "/api/stocks/research/backtest",
            json={"start_date": "2025-02-02", "end_date": "2025-11-16"},
        )
    finally:
        client.app.dependency_overrides.pop(stocks_research.get_stock_repository, None)
    assert response.status_code == 200
    data = response.json()
    assert data["rebalance_count"] >= 2
    assert data["final_value"] > 0
    assert data["benchmark_kind"] == "equal_weight"
    assert data["strategy"]["total_return"] is not None
    assert data["validation"]["rank_ic_count"] >= 1
    assert data["curve"] and data["rebalances"]
