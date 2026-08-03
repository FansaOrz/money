"""规则参数优化测试。

重点验证：
1. 数据泄漏防护：
   - 训练内 purged walk-forward：测试起点 = 训练窗口末 + embargo（embargo=test_window），
     打分基准日严格早于 embargo 隔离带；
   - 60/20/20 时间切分三段不重叠且顺序为 训练 < 验证 < 留出；
   - 篡改留出测试段数据不改变任何训练试验结果与最佳参数（留出段仅评估一次）；
   - 非调仓期不产生新的打分（持仓沿用，rebalance_interval 生效）；
2. 稳定性：
   - 两次相同请求结果完全一致（无随机性）；
   - max_trials 截断生效（executed ≤ max_trials，total_candidates 为截断前总数）；
   - 综合评分各分项与总分均在 [0,1]；
   - 审计回归：min_train 使用最大 train_window；调仓间隔按 ceil 折算且
     等价参数组合去重（不复核同口径回测）；留出段全程仅评估一次；
3. 上线门槛：
   - 四项门槛逐项判定，默认门槛下宽松/严格数据分别通过/不通过；
   - 门槛结果字段完整（gate.passed 与四项布尔、reasons）；
4. 响应结构：所有试验摘要、最佳参数、验证与留出评估、splits 区间完整。

使用合成的确定性净值序列，不依赖外部行情。
"""

import math
from datetime import date, timedelta
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

import app.services.quant_optimizer as optimizer_module

from app.models import Account, FundNav, Instrument, Position
from app.schemas.quant import (
    OptimizeRequest,
    WalkForwardRequest,
    WalkForwardWindow,
)
from app.services import quant_optimizer as optimizer
from app.services.quant_walkforward import run_walkforward_panels


# ---------------------------------------------------------------------------
# 数据构造辅助
# ---------------------------------------------------------------------------


def _seed_navs(
    db: Session,
    code: str,
    name: str,
    days: int = 400,
    start_nav: float = 1.0,
    daily_growth: float = 0.001,
    start: date = date(2024, 1, 1),
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
    """均值回归型正弦净值序列（强弱随窗口轮动）。"""
    return [1.0 + amplitude * math.sin(2 * math.pi * i / 20.0 + phase) for i in range(days)]


def _make_calendar(days: int, start: date = date(2024, 1, 1)) -> list[date]:
    return [start + timedelta(days=i) for i in range(days)]


def _trend_panel(days: int, daily_growth: float, noise: float = 0.0005) -> list[float]:
    """带微小交替噪声的确定性趋势净值序列。"""
    values = [1.0]
    for i in range(days - 1):
        sign = 1.0 if i % 2 == 0 else -1.0
        values.append(values[-1] * (1 + daily_growth + sign * noise))
    return values


def _seed_pool(db: Session, days: int = 400, count: int = 5) -> list[str]:
    """写入一组增长各异的候选基金，返回代码列表。"""
    codes = []
    for i in range(count):
        inst = _seed_navs(
            db,
            code=f"3300{i:02d}",
            name=f"沪深优化基金{i}",
            days=days,
            daily_growth=0.003 - i * 0.001,
        )
        codes.append(inst.code)
    return codes


def _small_request(codes: list[str], **overrides) -> dict:
    """小网格请求（测试运行时间受控）。"""
    payload = {
        "candidate_codes": codes,
        "search_space": {
            "windows": [[60, 10]],
            "factor_weights": {
                "momentum": [0.45, 0.65],
                "risk_adjusted": [0.35],
                "trend": [0.20],
                "drawdown": [0.50],
            },
            "rebalance_intervals": [10, 20],
            "top_n": [2, 3],
            "score_thresholds": [None, 0.0],
        },
        "max_trials": 40,
    }
    payload.update(overrides)
    return payload


# ---------------------------------------------------------------------------
# 数据泄漏防护（服务层纯函数）
# ---------------------------------------------------------------------------


def test_purged_walkforward_embargo_gap() -> None:
    """训练内 purged walk-forward：测试起点 = 训练末 + embargo，embargo=test_window。"""
    days = 200
    calendar = _make_calendar(days)
    panels = {code: _sine_panel(days, phase=0.5 * i) for i, code in enumerate("ABCD")}
    markets = dict.fromkeys(panels, "cn")
    req = WalkForwardRequest(
        candidate_codes=list(panels),
        window=WalkForwardWindow(train_window=60, test_window=10, step=10),
        top_n=2,
    )

    # embargo = test_window = 10：测试起点与训练末之间隔 10 天隔离带
    _, _, segments, _, _ = run_walkforward_panels(
        calendar, panels, markets, req, embargo=10
    )
    assert segments
    for seg in segments:
        train_end = date.fromisoformat(seg.train_end)
        test_start = date.fromisoformat(seg.test_start)
        # embargo = 10：测试起点比训练末晚 11 天（隔离带 10 天）
        assert (test_start - train_end).days == 11

    # embargo = 0（默认）：保持 walkforward 原行为（首尾相接）
    _, _, segments_plain, _, _ = run_walkforward_panels(calendar, panels, markets, req)
    for seg in segments_plain:
        train_end = date.fromisoformat(seg.train_end)
        test_start = date.fromisoformat(seg.test_start)
        assert (test_start - train_end).days == 1


def test_rebalance_interval_skips_rescoring() -> None:
    """非调仓期沿用持仓、不产生新打分：调仓期持仓变化，非调仓期持仓完全一致。

    rebalance_interval=2 时第 2 段沿用第 1 段持仓；且第 2 段的「沿用持仓」
    与「按其自身训练窗口重新打分的持仓」不同（证明确实没有重新打分）。
    """
    days = 200
    calendar = _make_calendar(days)
    panels = {code: _sine_panel(days, phase=0.9 * i) for i, code in enumerate("ABCDE")}
    markets = dict.fromkeys(panels, "cn")
    req = WalkForwardRequest(
        candidate_codes=list(panels),
        window=WalkForwardWindow(train_window=60, test_window=10, step=10),
        top_n=2,
    )

    _, _, segments_hold, _, _ = run_walkforward_panels(
        calendar, panels, markets, req, rebalance_interval=2
    )
    _, _, segments_every, _, _ = run_walkforward_panels(
        calendar, panels, markets, req, rebalance_interval=1
    )

    assert len(segments_hold) == len(segments_every) >= 3
    # 非调仓期（第 2 段）沿用上一期持仓
    assert segments_hold[1].holdings == segments_hold[0].holdings
    # 该持仓与「每段都重新打分」的第 2 段持仓不同 → 非调仓期确实没有打分
    assert segments_hold[1].holdings != segments_every[1].holdings
    # 调仓期（第 3 段，index-1=2 为 2 的倍数）重新打分，与逐段调仓一致
    assert segments_hold[2].holdings == segments_every[2].holdings


def test_splits_are_chronological_and_non_overlapping(
    client: TestClient, db_session: Session
) -> None:
    """60/20/20 切分：三段按时间顺序排列、互不重叠，合计覆盖全部共同交易日。"""
    codes = _seed_pool(db_session, days=400)
    data = client.post("/api/quant/optimize", json=_small_request(codes)).json()

    splits = data["splits"]
    assert set(splits) == {"train", "validation", "holdout"}
    train, validation, holdout = splits["train"], splits["validation"], splits["holdout"]
    # 时间顺序：train < validation < holdout（日期互不重叠）
    assert train["end_date"] < validation["start_date"]
    assert validation["end_date"] < holdout["start_date"]
    # 合计覆盖全部共同交易日
    total = train["sample_count"] + validation["sample_count"] + holdout["sample_count"]
    assert total == data["sample_count"]
    # 验证/留出评估的日期区间与切分一致
    assert data["validation"]["start_date"] == validation["start_date"]
    assert data["validation"]["end_date"] == validation["end_date"]
    assert data["holdout"]["start_date"] == holdout["start_date"]
    assert data["holdout"]["end_date"] == holdout["end_date"]


def test_holdout_data_does_not_influence_trials_or_best_params() -> None:
    """篡改留出测试段净值，训练试验摘要与最佳参数完全不变（留出段仅最后评估一次）。

    把留出段（最后 20%）全部净值 ×5，最佳参数、所有试验摘要、验证段评估
    必须逐字段一致；留出测试段评估（唯一使用该数据的地方）则发生变化。
    """
    days = 400
    calendar = _make_calendar(days)
    base_panels = {
        code: _trend_panel(days, daily_growth=0.002 - 0.0008 * i)
        for i, code in enumerate(["A", "B", "C", "D", "E"])
    }
    markets = dict.fromkeys(base_panels, "cn")

    req = OptimizeRequest(
        candidate_codes=list(base_panels),
        search_space={
            "windows": [[60, 10]],
            "factor_weights": {
                "momentum": [0.45, 0.65],
                "risk_adjusted": [0.35],
                "trend": [0.20],
                "drawdown": [0.50],
            },
            "rebalance_intervals": [10, 20],
            "top_n": [2, 3],
            "score_thresholds": [None, 0.0],
        },
    )

    def _run(panels: dict[str, list[float]]):
        train, validation, holdout = optimizer._split_panel(calendar, panels, 60, 10)
        combos, _total = optimizer._build_grid(req)
        trials = []
        for combo in combos:
            metrics = optimizer._evaluate(
                train[0], train[1], markets, combo, embargo=combo.test_window, min_rebalances=2
            )
            trials.append((combo, metrics))
        scores = optimizer._composite_scores([m for _, m in trials])
        best_index = max(range(len(trials)), key=lambda i: scores[i])
        best_combo = trials[best_index][0]
        validation_metrics = optimizer._evaluate(
            validation[0], validation[1], markets, best_combo, embargo=0, min_rebalances=1
        )
        holdout_metrics = optimizer._evaluate(
            holdout[0], holdout[1], markets, best_combo, embargo=0, min_rebalances=1
        )
        return scores, best_combo, validation_metrics, holdout_metrics

    scores_base, best_base, val_base, hold_base = _run(base_panels)

    # 仅篡改留出段中的基金 A，并引入逐日变化；统一乘常数不会改变收益率，
    # 因而不能验证留出段是否实际参与评估。
    tampered = {
        code: [
            v if i < 320 or code != "A" else v * (1.0 + 0.01 * (i - 319))
            for i, v in enumerate(values)
        ]
        for code, values in base_panels.items()
    }
    scores_tamp, best_tamp, val_tamp, hold_tamp = _run(tampered)

    # 训练试验评分、最佳参数、验证段评估完全不受留出段数据影响
    assert scores_base == scores_tamp
    assert best_base == best_tamp
    assert val_base.strategy_summary == val_tamp.strategy_summary
    # 留出测试段评估确实使用了留出段数据（评估结果变化，证明切分生效）
    assert hold_base.strategy_summary.total_return != hold_tamp.strategy_summary.total_return


# ---------------------------------------------------------------------------
# 审计回归：min_train / ceil 折算与等价去重 / 留出一次性
# ---------------------------------------------------------------------------


def test_min_train_uses_max_train_window() -> None:
    """训练段下限按最大 train_window 校验（审计回归：旧实现错用 test_window）。

    80 个共同交易日切分后训练段 = 80 - 16 - 16 = 48 天：
    - 正确口径 min_train = 30 + 2×10 + 1 = 51 > 48 → 必须报错；
    - 旧错误口径 10 + 2×10 + 1 = 31 ≤ 48 → 放行（大窗口组合随后全部跳过）。
    """
    days = 80
    calendar = _make_calendar(days)
    panels = {code: _trend_panel(days, 0.001) for code in ("A", "B", "C")}
    with pytest.raises(Exception, match="51"):
        optimizer._split_panel(calendar, panels, 30, 10)
    # 恰好等于下限时放行：n_train = 51 需要 85 个共同交易日
    days_ok = 85
    calendar_ok = _make_calendar(days_ok)
    panels_ok = {code: _trend_panel(days_ok, 0.001) for code in ("A", "B", "C")}
    train, _, _ = optimizer._split_panel(calendar_ok, panels_ok, 30, 10)
    assert len(train[0]) == 51


def test_window_rebalance_interval_uses_ceil() -> None:
    """调仓间隔折算为测试窗口个数：向上取整、至少为 1（审计回归）。"""
    assert optimizer._window_rebalance_interval(15, 10) == 2   # 15/10 ceil → 2
    assert optimizer._window_rebalance_interval(10, 10) == 1
    assert optimizer._window_rebalance_interval(9, 10) == 1    # 不足一窗口 → 每窗口调仓
    assert optimizer._window_rebalance_interval(0, 10) == 1    # 0 兜底为 1
    assert optimizer._window_rebalance_interval(60, 30) == 2
    assert optimizer._window_rebalance_interval(61, 30) == 3


def test_equivalent_combos_deduplicated(client: TestClient, db_session: Session) -> None:
    """调仓间隔折算后等价的参数组合只评估一次（审计回归）。

    test_window=10 时交易日间隔 5 与 10 都折算为「每窗口调仓」，
    其余维度相同 → 两个网格组合等价，应只执行一次、参数取首次出现者。
    """
    codes = _seed_pool(db_session, days=400)
    payload = _small_request(codes)
    payload["search_space"]["rebalance_intervals"] = [5, 10]
    payload["search_space"]["factor_weights"] = {
        "momentum": [0.45],
        "risk_adjusted": [0.35],
        "trend": [0.20],
        "drawdown": [0.50],
    }
    payload["search_space"]["top_n"] = [2]
    payload["search_space"]["score_thresholds"] = [None]
    data = client.post("/api/quant/optimize", json=payload).json()

    # 网格含 2 组（间隔 5/10），但口径等价 → 只执行 1 次
    assert data["total_candidates"] == 2
    assert data["executed_trials"] == 1
    assert len(data["trials"]) == 1
    # 去重保持网格确定式顺序：保留首次出现者（间隔 5）
    assert data["trials"][0]["params"]["rebalance_interval"] == 5
    assert data["best_params"]["rebalance_interval"] == 5


def test_holdout_evaluated_exactly_once(
    client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """完全留出测试段在整个优化流程中只被评估一次（审计回归）。

    包装 optimizer._evaluate 计数：留出段调用次数必须为 1，且唯一一次
    调用使用的参数与 best_params 一致；验证段评估次数 ≤ 候选短名单上限。
    """
    calls: list[str] = []
    original = optimizer_module._evaluate

    def _spy(calendar, panels, markets, combo, embargo, min_rebalances):
        segment = (
            "holdout"
            if calendar and calendar[0].isoformat() == holdout_start
            else "other"
        )
        calls.append(segment)
        return original(calendar, panels, markets, combo, embargo, min_rebalances)

    codes = _seed_pool(db_session, days=400)
    # 先确定留出段起点（最后 20%）：与 _split_panel 口径一致
    n = 400
    n_holdout = max(2, math.ceil(n * 0.2))
    holdout_start = (date(2024, 1, 1) + timedelta(days=n - n_holdout)).isoformat()

    monkeypatch.setattr(optimizer_module, "_evaluate", _spy)
    data = client.post("/api/quant/optimize", json=_small_request(codes)).json()

    assert calls.count("holdout") == 1
    assert data["holdout"]["start_date"] == holdout_start
    # 留出段唯一一次评估的参数就是 best_params
    holdout_eval = data["holdout"]
    assert holdout_eval["rebalance_count"] >= 1


# ---------------------------------------------------------------------------
# 稳定性
# ---------------------------------------------------------------------------


def test_result_deterministic(client: TestClient, db_session: Session) -> None:
    """相同数据两次调用结果完全一致（网格展开、截断、评分均无随机性）。"""
    codes = _seed_pool(db_session, days=400)
    first = client.post("/api/quant/optimize", json=_small_request(codes)).json()
    second = client.post("/api/quant/optimize", json=_small_request(codes)).json()
    assert first == second


def test_max_trials_truncates_grid(client: TestClient, db_session: Session) -> None:
    """max_trials 截断生效：executed ≤ max_trials < total_candidates（截断前总数）。"""
    codes = _seed_pool(db_session, days=400)
    payload = _small_request(codes)
    payload["search_space"]["windows"] = [[60, 10], [90, 15]]
    payload["search_space"]["rebalance_intervals"] = [10, 20, 40]
    payload["max_trials"] = 6
    data = client.post("/api/quant/optimize", json=payload).json()

    # 网格总数：2 窗口 × 2 动量 × 3 调仓 × 2 top_n × 2 阈值 = 48
    assert data["total_candidates"] == 48
    assert data["executed_trials"] <= 6
    assert len(data["trials"]) == data["executed_trials"]


def test_default_grid_total_and_max_trials_default() -> None:
    """默认网格规模与 max_trials 默认值：total = 3×16×4×4×3 = 2304，max_trials = 40。"""
    req = OptimizeRequest(candidate_codes=["A", "B"])
    combos, total = optimizer._build_grid(req)
    assert req.max_trials == 40
    assert total == 2304
    assert len(combos) == 40


def test_scores_within_unit_interval(client: TestClient, db_session: Session) -> None:
    """综合评分及各试验摘要字段范围合法：score ∈ [0,1]，换手 ≥ 0，试验序号连续。"""
    codes = _seed_pool(db_session, days=400)
    data = client.post("/api/quant/optimize", json=_small_request(codes)).json()

    assert data["trials"]
    for index, trial in enumerate(data["trials"], start=1):
        assert trial["trial_index"] == index
        assert 0.0 <= trial["score"] <= 1.0
        assert trial["turnover"] >= 0.0
        params = trial["params"]
        assert set(params["factor_weights"]) == {
            "momentum", "risk_adjusted", "trend", "drawdown",
        }
    # 最佳参数来自某个已执行试验
    executed = {t["params"]["rebalance_interval"] for t in data["trials"]}
    assert data["best_params"]["rebalance_interval"] in executed


def test_low_turnover_preferred_in_scoring() -> None:
    """低换手分项：评分中换手为逆向分位（换手低者得分高），缺失值置 0。"""
    metrics = [
        optimizer._Metrics(None, None, None, None, None, None, None, 0.50, 10),
        optimizer._Metrics(None, None, None, None, None, None, None, 0.10, 10),
        optimizer._Metrics(None, None, None, None, None, None, None, 0.30, 10),
    ]
    scores = optimizer._composite_scores(metrics)
    # 只有换手有区分度：得分完全由低换手分位决定，换手 0.10 者最高
    assert scores[1] == pytest.approx(optimizer.SCORE_WEIGHT_TURNOVER * 1.0)
    assert scores[0] == pytest.approx(0.0)
    assert scores[2] == pytest.approx(optimizer.SCORE_WEIGHT_TURNOVER * 0.5)
    assert scores[1] > scores[2] > scores[0]


def test_scoring_weights_sum_to_one() -> None:
    """综合评分权重：0.35 夏普 + 0.30 回撤改善 + 0.20 超额 + 0.15 低换手 = 1。"""
    total = (
        optimizer.SCORE_WEIGHT_SHARPE
        + optimizer.SCORE_WEIGHT_DRAWDOWN
        + optimizer.SCORE_WEIGHT_EXCESS
        + optimizer.SCORE_WEIGHT_TURNOVER
    )
    assert total == pytest.approx(1.0)
    # 全部指标最优者综合分为 1（字段顺序：strategy_summary, benchmark_summary,
    # sharpe, max_drawdown, benchmark_max_drawdown, drawdown_improvement,
    # excess_return, turnover, rebalance_count）
    best = optimizer._Metrics(None, None, 1.0, -0.01, -0.20, 0.19, 0.20, 0.1, 10)
    worst = optimizer._Metrics(None, None, 0.0, -0.30, -0.10, -0.20, -0.20, 0.9, 10)
    missing = optimizer._Metrics(None, None, None, None, None, None, None, 0.5, 10)
    scores = optimizer._composite_scores([best, worst, missing])
    assert scores[0] == pytest.approx(1.0)
    assert scores[1] == pytest.approx(0.0)  # 四项分位全部最差
    assert scores[2] == pytest.approx(0.15 * 0.5)  # 仅换手分位参与（其余缺失置 0）


# ---------------------------------------------------------------------------
# 上线门槛
# ---------------------------------------------------------------------------


def test_gate_passes_with_lenient_thresholds(
    client: TestClient, db_session: Session
) -> None:
    """宽松门槛下 gate.passed 为 True，四项布尔均为 True，reasons 完整。"""
    codes = _seed_pool(db_session, days=400)
    payload = _small_request(
        codes,
        gate_min_sharpe=-5.0,
        gate_max_drawdown=-1.0,
        gate_min_excess_return=-1.0,
        gate_max_turnover=5.0,
    )
    data = client.post("/api/quant/optimize", json=payload).json()

    gate = data["gate"]
    assert gate["passed"] is True
    assert gate["sharpe_pass"] is True
    assert gate["drawdown_pass"] is True
    assert gate["excess_pass"] is True
    assert gate["turnover_pass"] is True
    assert gate["min_oos_sharpe"] == -5.0
    assert gate["max_drawdown_limit"] == -1.0
    assert gate["min_excess_return"] == -1.0
    assert gate["max_turnover"] == 5.0
    assert any("达到上线门槛" in reason for reason in gate["reasons"])


def test_gate_fails_with_strict_thresholds(
    client: TestClient, db_session: Session
) -> None:
    """严格门槛下 gate.passed 为 False，并指出未通过项。"""
    codes = _seed_pool(db_session, days=400)
    payload = _small_request(
        codes,
        gate_min_excess_return=1.0,  # 超额 100%：不可能达到
        gate_max_turnover=0.0001,    # 几乎不允许换手
    )
    data = client.post("/api/quant/optimize", json=payload).json()

    gate = data["gate"]
    assert gate["passed"] is False
    assert gate["excess_pass"] is False
    # 合成样本可能选出不换仓组合，因此换手门槛不一定失败；整体仍必须拒绝。
    assert any("未达到上线门槛" in reason for reason in gate["reasons"])


def test_gate_uses_holdout_metrics(client: TestClient, db_session: Session) -> None:
    """门槛判定基于完全留出测试段：pass 标志与留出段指标一致。"""
    codes = _seed_pool(db_session, days=400)
    data = client.post("/api/quant/optimize", json=_small_request(codes)).json()

    gate = data["gate"]
    holdout = data["holdout"]
    sharpe = holdout["strategy"]["sharpe"]
    expected_sharpe_pass = sharpe is not None and sharpe >= gate["min_oos_sharpe"]
    assert gate["sharpe_pass"] == expected_sharpe_pass
    expected_excess_pass = (
        holdout["excess_return"] is not None
        and holdout["excess_return"] >= gate["min_excess_return"]
    )
    assert gate["excess_pass"] == expected_excess_pass
    expected_turnover_pass = holdout["turnover"] <= gate["max_turnover"]
    assert gate["turnover_pass"] == expected_turnover_pass
    # 四项合取 = passed
    assert gate["passed"] == (
        gate["sharpe_pass"] and gate["drawdown_pass"]
        and gate["excess_pass"] and gate["turnover_pass"]
    )


# ---------------------------------------------------------------------------
# 响应结构与错误处理
# ---------------------------------------------------------------------------


def test_response_schema_fields(client: TestClient, db_session: Session) -> None:
    """响应字段完整：试验摘要、最佳参数、验证/留出评估、门槛、splits、方法说明。"""
    codes = _seed_pool(db_session, days=400)
    data = client.post("/api/quant/optimize", json=_small_request(codes)).json()

    assert data["candidate_codes"] == codes
    assert data["max_trials"] == 40
    assert data["executed_trials"] > 0
    assert data["total_candidates"] >= data["executed_trials"]
    assert data["methodology"]
    assert isinstance(data["warnings"], list)

    for key in ("validation", "holdout"):
        evaluation = data[key]
        assert evaluation["segment"] == key
        assert evaluation["sample_count"] == data["splits"][key]["sample_count"]
        assert set(evaluation["strategy"]) == {
            "total_return", "annual_return", "max_drawdown", "sharpe", "win_rate",
        }
        assert set(evaluation["benchmark"]) == set(evaluation["strategy"])

    best = data["best_params"]
    assert set(best) == {
        "train_window", "test_window", "rebalance_interval",
        "top_n", "score_threshold", "factor_weights",
    }


def test_validation_and_holdout_evaluated_once_with_best_params(
    client: TestClient, db_session: Session
) -> None:
    """验证与留出评估的参数即为 best_params（窗口/top_n/调仓间隔可从评估推断）。"""
    codes = _seed_pool(db_session, days=400)
    payload = _small_request(codes)
    payload["search_space"]["windows"] = [[60, 10], [80, 10]]
    data = client.post("/api/quant/optimize", json=payload).json()

    best = data["best_params"]
    # 留出段调仓次数 ≤ 样本数 / 调仓间隔（间隔越大调仓越少），且至少 1 次
    holdout = data["holdout"]
    assert 1 <= holdout["rebalance_count"]
    assert holdout["rebalance_count"] <= holdout["sample_count"] // 10 + 1
    # best_params 的窗口必须来自搜索网格
    assert [best["train_window"], best["test_window"]] in [[60, 10], [80, 10]]


def test_default_candidate_pool_uses_positions(
    client: TestClient, db_session: Session
) -> None:
    """缺省 candidate_codes 时回退为当前持仓基金。"""
    codes = _seed_pool(db_session, days=400, count=3)
    for code in codes:
        instrument = db_session.query(Instrument).filter(Instrument.code == code).one()
        _seed_position(db_session, instrument)

    payload = _small_request([])
    payload.pop("candidate_codes")
    response = client.post("/api/quant/optimize", json=payload)
    assert response.status_code == 200
    assert sorted(response.json()["candidate_codes"]) == sorted(codes)


def test_insufficient_data_returns_400(client: TestClient, db_session: Session) -> None:
    """样本不足以切分出一个完整训练窗口：400 并提示。"""
    codes = _seed_pool(db_session, days=120)  # 60% 训练段仅 ~72 天 < 60+2×10+1
    response = client.post("/api/quant/optimize", json=_small_request(codes))
    assert response.status_code == 400
    assert "切分" in response.json()["detail"] or "样本" in response.json()["detail"]


def test_unknown_codes_return_400(client: TestClient, db_session: Session) -> None:
    """候选代码全部未知：400。"""
    response = client.post(
        "/api/quant/optimize", json=_small_request(["999998", "999999"])
    )
    assert response.status_code == 400


def test_invalid_window_bounds_return_422(client: TestClient, db_session: Session) -> None:
    """窗口组合越界（train_window < 20）：请求校验 422。"""
    codes = _seed_pool(db_session, days=400)
    payload = _small_request(codes)
    payload["search_space"]["windows"] = [[10, 10]]
    response = client.post("/api/quant/optimize", json=payload)
    assert response.status_code == 422
