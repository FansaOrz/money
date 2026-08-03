"""规则参数优化：时间序列切分 + 训练内 purged walk-forward + 有限网格搜索。

流程（全部为只读研究能力，不产生任何实盘下单行为）：

1. 数据：装载候选基金 FundNav 历史净值（优先累计净值，缺失回退单位净值），
   对齐到共同交易日交集（复用 quant_walkforward._load_aligned_panels）；
2. 切分：按时间先后 60% / 20% / 20% 切分为训练段 / 验证段 / 完全留出测试段，
   三段不重叠；完全留出测试段仅在最后对最佳参数评估一次；
3. 搜索：有限网格（窗口组合 train×test、因子权重组、调仓间隔
   10/20/40/60、top_n 5/10/15/20、综合分阈值组）的笛卡尔积，
   为控制运行时间按 max_trials（默认 40）确定式截断；
   调仓间隔按 ceil(交易日 ÷ test_window) 折算为测试窗口个数，
   折算后完全等价的参数组合只评估一次（确定式去重）；
4. 训练评估：每组参数在训练段内做 purged walk-forward ——
   段间间隔 embargo = test_window（训练窗口与样本外测试窗口之间的
   隔离带，防止相邻窗口信息泄漏），步进 step = test_window；
   打分只使用训练窗口内的数据（复用 quant_walkforward.run_walkforward_panels，
   因子权重与入选阈值已参数化）；
5. 评分：对全部试验的训练段样本外指标做横截面分位排名，综合分 =
   0.35×样本外夏普分位 + 0.30×回撤改善分位 + 0.20×超额收益分位
   + 0.15×低换手分位（分位 ∈ [0,1]，缺失值分位置 0）；
6. 选择：按综合分取前若干（≤5）组参数在验证段各评估一次，选验证段
   综合分（同一加权口径，对候选集重新分位）最高者为最佳参数；
7. 留出测试：最佳参数在完全留出测试段评估一次（仅一次），输出策略/
   基准指标、超额收益、回撤改善与换手率；
8. 上线门槛：基于完全留出测试段判定 —— 样本外夏普 ≥ 下限、
   最大回撤不差于下限、超额收益 ≥ 下限、平均换手率 ≤ 上限，
   四项全部满足视为达到上线门槛。

无未来数据保证：
- 训练/验证/留出三段按时间顺序互不重叠，留出段只在选定最佳参数后评估一次；
- 训练内 walk-forward 每段打分只用训练窗口内数据，且测试窗口与下一个
  训练窗口之间留有 embargo = test_window 的隔离带（purged）；
- 网格评分/验证/留出评估的全部指标都由同一只读回测引擎产出。

仅使用标准库 + SQLAlchemy；数据源为 FundNav。
"""

from __future__ import annotations

import itertools
import math
from dataclasses import dataclass
from datetime import date
from typing import Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Instrument
from app.schemas.quant import (
    OptimizeEvaluation,
    OptimizeGateStatus,
    OptimizeParamSet,
    OptimizeRequest,
    OptimizeResult,
    OptimizeTrialSummary,
    WalkForwardRequest,
    WalkForwardSummary,
    WalkForwardWindow,
)
from app.services import quant_factors as factors
from app.services.quant import QuantError
from app.services.quant_walkforward import (
    FactorWeights,
    _load_aligned_panels,
    _summarize,
    run_walkforward_panels,
)

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

TRAIN_RATIO = 0.6  # 训练段占比（按时间先后）
VALIDATION_RATIO = 0.2  # 验证段占比
HOLDOUT_RATIO = 0.2  # 完全留出测试段占比

# 综合评分权重：样本外夏普 / 回撤改善 / 超额收益 / 低换手
SCORE_WEIGHT_SHARPE = 0.35
SCORE_WEIGHT_DRAWDOWN = 0.30
SCORE_WEIGHT_EXCESS = 0.20
SCORE_WEIGHT_TURNOVER = 0.15

VALIDATION_SHORTLIST = 5  # 训练评分前列进入验证段比较的最大参数组数
MAX_DATA_WARNINGS = 40  # 数据装载阶段警告的最大透传条数（控制响应规模）

OPTIMIZE_METHODOLOGY = (
    "规则参数优化（只读研究，不产生任何实盘下单）："
    "数据按时间先后 60%/20%/20% 切分为训练/验证/完全留出测试三段（不重叠）。"
    "在训练段内做 purged walk-forward：train_window 个净值样本训练打分、"
    "随后 test_window 个样本样本外测试，步进 step = test_window，"
    "段间 embargo = test_window（训练窗口与下一测试窗口之间的隔离带）。"
    "搜索有限网格：窗口组合 (train_window × test_window)、因子权重组"
    "（z(动量)/z(风险调整动量)/趋势/z(回撤) 的加权系数）、调仓间隔 10/20/40/60、"
    "top_n 5/10/15/20、综合分入选阈值组；为控制运行时间按 max_trials（默认 40）"
    "确定式截断（窗口优先 + 分层等距抽样，顺序确定、无随机性）。"
    "调仓间隔按测试窗口个数折算：ceil(间隔交易日 ÷ test_window)，不足一个"
    "测试窗口时每个测试窗口都调仓；折算后等价的参数组合只评估一次（去重）。"
    "综合评分 = 0.35×样本外夏普分位 + 0.30×回撤改善分位 + 0.20×超额收益分位 "
    "+ 0.15×低换手分位（对全部试验横截面排名，缺失值分位置 0）。"
    "训练评分前 ≤5 组参数在验证段各评估一次，按同一口径综合分选出最佳参数；"
    "最佳参数在完全留出测试段仅评估一次。"
    "上线门槛（均可由请求覆盖）：留出测试段夏普 ≥ 0.5、最大回撤不差于 -25%、"
    "超额收益 ≥ 0、平均换手率 ≤ 100%，四项全部满足视为达到上线门槛。"
    "回测口径与 Walk-Forward 一致：打分仅用训练窗口内数据，选 top_n 只按综合分"
    "归一目标权重（受单基金 ≤25%、单一市场 ≤50% 约束，截断部分保留为现金），"
    "测试期买入并持有；不卖空、不计手续费与滑点、现金零收益；"
    "基准为全部有效候选基金等权买入持有（B0）；年化按 252 个交易日折算，"
    "夏普比率采用 2% 无风险利率。仅为研究用途，不构成投资建议。"
)


# ---------------------------------------------------------------------------
# 参数组合与网格
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _ParamCombo:
    """网格中的一组规则参数。"""

    train_window: int
    test_window: int
    rebalance_interval: int
    top_n: int
    score_threshold: float | None
    factor_weights: FactorWeights

    def to_schema(self) -> OptimizeParamSet:
        return OptimizeParamSet(
            train_window=self.train_window,
            test_window=self.test_window,
            rebalance_interval=self.rebalance_interval,
            top_n=self.top_n,
            score_threshold=self.score_threshold,
            factor_weights=self.factor_weights.as_dict(),
        )


def _stratified_subsample(items: list, limit: int) -> list:
    """确定式分层等距抽样：limit ≥ 长度时原样返回，否则等距取 limit 个（含首尾）。"""
    if limit >= len(items):
        return list(items)
    if limit <= 1:
        return [items[0]]
    step = (len(items) - 1) / (limit - 1)
    indices = sorted({round(i * step) for i in range(limit)})
    return [items[i] for i in indices]


def _build_grid(req: OptimizeRequest) -> tuple[list[_ParamCombo], int]:
    """展开搜索网格并按 max_trials 确定式截断。

    截断策略（窗口优先 + 分层抽样，保证各窗口组合与调仓间隔都被覆盖）：
    1. 按 windows × rebalance_intervals 分成若干桶（顺序保持输入顺序）；
    2. 每桶分到 ceil(max_trials / 桶数) 个名额，桶内参数组合（因子权重 ×
       top_n × 阈值）分层等距抽样；
    3. 若总额超出 max_trials，对拼接结果再做一次分层等距抽样到 max_trials。

    返回 (待执行参数组合, 网格总数)。
    """
    space = req.search_space
    weight_combos = [
        FactorWeights(momentum=m, risk_adjusted=r, trend=t, drawdown=d)
        for m, r, t, d in itertools.product(
            space.factor_weights.momentum,
            space.factor_weights.risk_adjusted,
            space.factor_weights.trend,
            space.factor_weights.drawdown,
        )
    ]
    inner = [
        (weights, top_n, threshold)
        for weights, top_n, threshold in itertools.product(
            weight_combos, space.top_n, space.score_thresholds
        )
    ]
    buckets: list[tuple[int, int, int]] = [
        (train_window, test_window, interval)
        for (train_window, test_window), interval in itertools.product(
            space.windows, space.rebalance_intervals
        )
    ]
    total = len(buckets) * len(inner)
    if total <= req.max_trials:
        return [
            _ParamCombo(tw, sw, interval, top_n, threshold, weights)
            for tw, sw, interval in buckets
            for weights, top_n, threshold in inner
        ], total

    per_bucket = max(1, math.ceil(req.max_trials / len(buckets)))
    sampled: list[_ParamCombo] = []
    for train_window, test_window, interval in buckets:
        for weights, top_n, threshold in _stratified_subsample(inner, per_bucket):
            sampled.append(
                _ParamCombo(train_window, test_window, interval, top_n, threshold, weights)
            )
    if len(sampled) > req.max_trials:
        sampled = _stratified_subsample(sampled, req.max_trials)
    return sampled, total


def _window_rebalance_interval(rebalance_interval: int, test_window: int) -> int:
    """调仓间隔（交易日）折算为测试窗口个数（向上取整，至少 1）。

    ceil 保证实际调仓间隔 ≥ 指定的交易日数（不足的按每窗口调仓处理）；
    注意折算结果相同的不同「交易日间隔」参数在回测口径下完全等价。
    """
    return max(1, math.ceil(rebalance_interval / test_window))


# ---------------------------------------------------------------------------
# 时间切分（60% / 20% / 20%）
# ---------------------------------------------------------------------------


def _split_panel(
    calendar: list[date],
    panels: dict[str, list[float]],
    train_window_max: int,
    test_window_max: int,
) -> tuple[
    tuple[list[date], dict[str, list[float]]],
    tuple[list[date], dict[str, list[float]]],
    tuple[list[date], dict[str, list[float]]],
]:
    """按时间先后 60/20/20 切分共同交易日与各基金净值面板。

    约束：训练段需容纳最大的 purged walk-forward 窗口
    （train_window_max + embargo=test_window_max + test_window_max，再加 1
    个样本外测试日），验证段需容纳 train_window + 1（至少一次调仓建仓 +
    一个样本外交易日），留出段至少 2 个样本（可计算收益）。
    """
    n = len(calendar)
    n_holdout = max(2, math.ceil(n * HOLDOUT_RATIO))
    n_validation = max(1, math.ceil(n * VALIDATION_RATIO))
    n_train = n - n_validation - n_holdout

    # 最大窗口组合：train_window_max + 2×test_window_max（embargo+测试）+ 1 个测试日
    min_train = train_window_max + 2 * test_window_max + 1
    if n_train < min_train:
        raise QuantError(
            f"共同交易日 {n} 天，按 60/20/20 切分后训练段仅 {n_train} 天，"
            f"不足以容纳最大的 purged walk-forward 窗口"
            f"（train_window+2×test_window+1 = {min_train} 天），"
            "请缩短窗口组合或延长净值区间"
        )

    def _slice(start: int, end: int) -> tuple[list[date], dict[str, list[float]]]:
        return calendar[start:end], {code: values[start:end] for code, values in panels.items()}

    train_end = n_train
    validation_end = n_train + n_validation
    return (
        _slice(0, train_end),
        _slice(train_end, validation_end),
        _slice(validation_end, n),
    )


def _split_info(
    train: tuple[list[date], dict[str, list[float]]],
    validation: tuple[list[date], dict[str, list[float]]],
    holdout: tuple[list[date], dict[str, list[float]]],
) -> dict[str, dict[str, str | int]]:
    """三段切分的日期区间与样本数（响应 splits 字段）。"""
    info: dict[str, dict[str, str | int]] = {}
    for name, (calendar, _panels) in (
        ("train", train),
        ("validation", validation),
        ("holdout", holdout),
    ):
        info[name] = {
            "start_date": calendar[0].isoformat(),
            "end_date": calendar[-1].isoformat(),
            "sample_count": len(calendar),
        }
    return info


# ---------------------------------------------------------------------------
# 单次评估（训练段 purged walk-forward / 验证段 / 留出段）
# ---------------------------------------------------------------------------


@dataclass
class _Metrics:
    """一次回测评估提炼出的指标。"""

    strategy_summary: WalkForwardSummary
    benchmark_summary: WalkForwardSummary
    sharpe: float | None
    max_drawdown: float | None
    benchmark_max_drawdown: float | None
    drawdown_improvement: float | None
    excess_return: float | None
    turnover: float
    rebalance_count: int


def _evaluate(
    calendar: list[date],
    panels: dict[str, list[float]],
    markets: dict[str, str],
    combo: _ParamCombo,
    embargo: int,
    min_rebalances: int,
) -> _Metrics:
    """在给定数据段上执行一次回测评估（只读，不访问数据库）。

    - step = test_window；embargo 为段间隔离带（训练段内 = test_window，
      验证/留出段 = 0，数据本身已按时间隔离）；
    - 窗口不足 min_rebalances 个调仓期时抛 QuantError（由调用方处理）。
    """
    req = WalkForwardRequest(
        candidate_codes=list(panels),
        window=WalkForwardWindow(
            train_window=combo.train_window,
            test_window=combo.test_window,
            step=combo.test_window,
        ),
        top_n=combo.top_n,
    )
    strategy, benchmark, segments, avg_turnover, _warnings = run_walkforward_panels(
        calendar,
        panels,
        markets,
        req,
        embargo=embargo,
        factor_weights=combo.factor_weights,
        score_threshold=combo.score_threshold,
        rebalance_interval=combo.rebalance_interval,
    )
    if len(segments) < min_rebalances:
        raise QuantError(
            f"该数据段仅产生 {len(segments)} 个调仓期（少于 {min_rebalances} 个），"
            "窗口参数与该段样本数不匹配"
        )
    strategy_summary = _summarize(strategy)
    benchmark_summary = _summarize(benchmark)
    excess = (
        strategy_summary.total_return - benchmark_summary.total_return
        if strategy_summary.total_return is not None
        and benchmark_summary.total_return is not None
        else None
    )
    drawdown_improvement = (
        strategy_summary.max_drawdown - benchmark_summary.max_drawdown
        if strategy_summary.max_drawdown is not None
        and benchmark_summary.max_drawdown is not None
        else None
    )
    return _Metrics(
        strategy_summary=strategy_summary,
        benchmark_summary=benchmark_summary,
        sharpe=strategy_summary.sharpe,
        max_drawdown=strategy_summary.max_drawdown,
        benchmark_max_drawdown=benchmark_summary.max_drawdown,
        drawdown_improvement=drawdown_improvement,
        excess_return=excess,
        turnover=avg_turnover,
        rebalance_count=len(segments),
    )


# ---------------------------------------------------------------------------
# 横截面分位评分（0.35 夏普 + 0.30 回撤改善 + 0.20 超额 + 0.15 低换手）
# ---------------------------------------------------------------------------


def _rank_fractions(values: list[float | None], higher_better: bool = True) -> list[float]:
    """横截面分位排名 ∈ [0,1]：最优者 1.0，最差者 0.0；并列取平均秩；None → 0.0。"""
    n = len(values)
    if n == 0:
        return []
    valid = sorted(v for v in values if v is not None)
    if not valid:
        return [0.0] * n
    fractions: list[float] = []
    for value in values:
        if value is None:
            fractions.append(0.0)
            continue
        worse = sum(1 for v in valid if v < value)
        equal = sum(1 for v in valid if v == value)
        # 并列取平均秩后归一：最优 ≈ 1.0，最差 = 0.0
        rank = (2 * worse + equal - 1) / 2  # 0 .. len(valid)-1
        fraction = rank / (len(valid) - 1) if len(valid) > 1 else 1.0
        fractions.append(fraction if higher_better else 1.0 - fraction)
    return fractions


def _composite_scores(metrics: list[_Metrics]) -> list[float]:
    """对一组评估指标计算综合评分（横截面分位加权）。"""
    sharpe_q = _rank_fractions([m.sharpe for m in metrics])
    drawdown_q = _rank_fractions([m.drawdown_improvement for m in metrics])
    excess_q = _rank_fractions([m.excess_return for m in metrics])
    turnover_q = _rank_fractions([m.turnover for m in metrics], higher_better=False)
    return [
        SCORE_WEIGHT_SHARPE * s + SCORE_WEIGHT_DRAWDOWN * d + SCORE_WEIGHT_EXCESS * e + SCORE_WEIGHT_TURNOVER * t
        for s, d, e, t in zip(sharpe_q, drawdown_q, excess_q, turnover_q, strict=True)
    ]


# ---------------------------------------------------------------------------
# 上线门槛
# ---------------------------------------------------------------------------


def _evaluate_gate(req: OptimizeRequest, holdout: _Metrics) -> OptimizeGateStatus:
    """基于完全留出测试段的单次评估判定上线门槛（四项全部满足）。"""
    sharpe_pass = holdout.sharpe is not None and holdout.sharpe >= req.gate_min_sharpe
    drawdown_pass = (
        holdout.max_drawdown is not None and holdout.max_drawdown >= req.gate_max_drawdown
    )
    excess_pass = (
        holdout.excess_return is not None and holdout.excess_return >= req.gate_min_excess_return
    )
    turnover_pass = holdout.turnover <= req.gate_max_turnover

    def _fmt(value: float | None) -> str:
        return "缺失" if value is None else f"{value:.4f}"

    reasons = [
        f"样本外夏普 {_fmt(holdout.sharpe)} {'≥' if sharpe_pass else '<'} 门槛 "
        f"{req.gate_min_sharpe:.2f}（{'通过' if sharpe_pass else '未通过'}）",
        f"最大回撤 {_fmt(holdout.max_drawdown)} {'不差于' if drawdown_pass else '差于'} 门槛 "
        f"{req.gate_max_drawdown:.2%}（{'通过' if drawdown_pass else '未通过'}）",
        f"超额收益 {_fmt(holdout.excess_return)} {'≥' if excess_pass else '<'} 门槛 "
        f"{req.gate_min_excess_return:.2%}（{'通过' if excess_pass else '未通过'}）",
        f"平均换手率 {holdout.turnover:.4f} {'≤' if turnover_pass else '>'} 门槛 "
        f"{req.gate_max_turnover:.2%}（{'通过' if turnover_pass else '未通过'}）",
    ]
    passed = sharpe_pass and drawdown_pass and excess_pass and turnover_pass
    reasons.append("四项门槛全部满足，达到上线门槛" if passed else "存在未通过门槛，暂未达到上线门槛")
    return OptimizeGateStatus(
        min_oos_sharpe=req.gate_min_sharpe,
        max_drawdown_limit=req.gate_max_drawdown,
        min_excess_return=req.gate_min_excess_return,
        max_turnover=req.gate_max_turnover,
        sharpe_pass=sharpe_pass,
        drawdown_pass=drawdown_pass,
        excess_pass=excess_pass,
        turnover_pass=turnover_pass,
        passed=passed,
        reasons=reasons,
    )


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------


def run_optimize(db: Session, req: OptimizeRequest) -> OptimizeResult:
    """规则参数优化入口：装载对齐 → 时间切分 → 网格搜索 → 验证 → 留出测试一次。"""
    # ---- 1. 数据装载（复用 walkforward 的对齐逻辑；按最大窗口过滤样本不足基金）----
    train_window_max = max(tw for tw, _sw in req.search_space.windows)
    test_window_max = max(sw for _tw, sw in req.search_space.windows)
    load_req = WalkForwardRequest(
        candidate_codes=req.candidate_codes,
        window=WalkForwardWindow(
            train_window=train_window_max,
            test_window=test_window_max,
            step=test_window_max,
        ),
        start_date=req.start_date,
        end_date=req.end_date,
    )
    calendar, panels, codes, warnings = _load_aligned_panels(db, load_req)
    if len(warnings) > MAX_DATA_WARNINGS:
        omitted = len(warnings) - MAX_DATA_WARNINGS
        warnings = warnings[:MAX_DATA_WARNINGS] + [f"其余 {omitted} 条数据提示已省略"]

    # ---- 2. 时间切分 60/20/20（不重叠；留出段仅最后评估一次）----
    train_part, validation_part, holdout_part = _split_panel(
        calendar, panels, train_window_max, test_window_max
    )
    train_calendar, train_panels = train_part
    validation_calendar, validation_panels = validation_part
    holdout_calendar, holdout_panels = holdout_part
    if len(validation_calendar) < train_window_max + 1:
        warnings.append(
            f"验证段样本 {len(validation_calendar)} 天，小于最大窗口组合所需 "
            f"{train_window_max + 1} 天，部分参数组合无法在验证段评估"
        )

    # 市场分类（单一市场 50% 约束）需要基金名称
    markets = {code: "cn" for code in codes}
    rows = db.execute(
        select(Instrument.code, Instrument.name).where(Instrument.code.in_(codes))
    ).all()
    for code, name in rows:
        markets[code] = factors.classify_market(name)

    # ---- 3. 网格展开与截断（max_trials 控制运行时间）----
    # 调仓间隔（交易日）按 ceil 折算为测试窗口个数（见 _window_rebalance_interval）；
    # 折算结果与其他维度完全相同的等价参数组合只评估第一次出现的（保持网格
    # 确定式顺序），避免同口径回测被重复执行、虚占试验名额与排名分位。
    combos, total_candidates = _build_grid(req)

    # ---- 4. 训练段 purged walk-forward 评估（embargo = test_window）----
    trials: list[tuple[_ParamCombo, _Metrics, float]] = []  # (参数, 指标, 综合分)
    trial_metrics: list[_Metrics] = []
    seen_effective: set[tuple] = set()
    for combo in combos:
        window_rebalance = _window_rebalance_interval(
            combo.rebalance_interval, combo.test_window
        )
        window_combo = _ParamCombo(
            combo.train_window,
            combo.test_window,
            window_rebalance,
            combo.top_n,
            combo.score_threshold,
            combo.factor_weights,
        )
        effective_key = (
            window_combo.train_window,
            window_combo.test_window,
            window_combo.rebalance_interval,
            window_combo.top_n,
            window_combo.score_threshold,
            window_combo.factor_weights,
        )
        if effective_key in seen_effective:
            continue  # 等价参数组合（调仓间隔折算相同）去重，仅评估首次出现者
        seen_effective.add(effective_key)
        try:
            metrics = _evaluate(
                train_calendar,
                train_panels,
                markets,
                window_combo,
                embargo=combo.test_window,
                min_rebalances=2,
            )
        except QuantError as exc:
            warnings.append(
                f"参数组合（train={combo.train_window}/test={combo.test_window}/"
                f"调仓={combo.rebalance_interval}/top_n={combo.top_n}）训练段评估跳过：{exc}"
            )
            continue
        trials.append((combo, metrics, 0.0))
        trial_metrics.append(metrics)
    if not trials:
        raise QuantError(
            "训练段样本不足以评估任何参数组合，请缩短窗口组合、减少 embargo 占用或延长净值区间"
        )

    scores = _composite_scores(trial_metrics)
    trials = [(combo, metrics, score) for (combo, metrics, _), score in zip(trials, scores, strict=True)]

    trial_summaries = [
        OptimizeTrialSummary(
            trial_index=index,
            params=combo.to_schema(),
            sharpe=metrics.sharpe,
            max_drawdown=metrics.max_drawdown,
            benchmark_max_drawdown=metrics.benchmark_max_drawdown,
            drawdown_improvement=metrics.drawdown_improvement,
            excess_return=metrics.excess_return,
            turnover=round(metrics.turnover, 6),
            score=round(score, 6),
        )
        for index, (combo, metrics, score) in enumerate(trials, start=1)
    ]

    # ---- 5. 验证段选最佳参数（训练评分前 ≤5 组，同口径综合分比较）----
    ranked = sorted(range(len(trials)), key=lambda i: (-trials[i][2], i))
    shortlist = ranked[:VALIDATION_SHORTLIST]
    validation_candidates: list[tuple[int, _ParamCombo, _Metrics]] = []
    for train_rank, i in enumerate(shortlist):
        combo = trials[i][0]
        window_combo = _ParamCombo(
            combo.train_window,
            combo.test_window,
            _window_rebalance_interval(combo.rebalance_interval, combo.test_window),
            combo.top_n,
            combo.score_threshold,
            combo.factor_weights,
        )
        try:
            metrics = _evaluate(
                validation_calendar,
                validation_panels,
                markets,
                window_combo,
                embargo=0,
                min_rebalances=1,
            )
        except QuantError:
            continue  # 窗口大于验证段样本的组合跳过
        validation_candidates.append((train_rank, combo, metrics))
    if not validation_candidates:
        raise QuantError(
            f"验证段样本 {len(validation_calendar)} 天不足以评估任何候选参数组合，"
            "请缩短窗口组合或延长净值区间"
        )
    validation_scores = _composite_scores([metrics for _, _, metrics in validation_candidates])
    # 验证段综合分最高者；并列时依次按低换手、训练评分更高者
    best_position = max(
        range(len(validation_candidates)),
        key=lambda i: (
            validation_scores[i],
            -validation_candidates[i][2].turnover,
            -validation_candidates[i][0],
        ),
    )
    _best_rank, best_combo, best_validation = validation_candidates[best_position]

    # ---- 6. 完全留出测试段：仅对最佳参数评估一次 ----
    best_holdout = _evaluate(
        holdout_calendar,
        holdout_panels,
        markets,
        _ParamCombo(
            best_combo.train_window,
            best_combo.test_window,
            _window_rebalance_interval(
                best_combo.rebalance_interval, best_combo.test_window
            ),
            best_combo.top_n,
            best_combo.score_threshold,
            best_combo.factor_weights,
        ),
        embargo=0,
        min_rebalances=1,
    )

    def _to_evaluation(
        segment: Literal["validation", "holdout"],
        part_calendar: list[date],
        metrics: _Metrics,
    ) -> OptimizeEvaluation:
        return OptimizeEvaluation(
            segment=segment,
            start_date=part_calendar[0].isoformat(),
            end_date=part_calendar[-1].isoformat(),
            sample_count=len(part_calendar),
            rebalance_count=metrics.rebalance_count,
            strategy=metrics.strategy_summary,
            benchmark=metrics.benchmark_summary,
            excess_return=metrics.excess_return,
            drawdown_improvement=metrics.drawdown_improvement,
            turnover=round(metrics.turnover, 6),
        )

    validation_eval = _to_evaluation("validation", validation_calendar, best_validation)
    holdout_eval = _to_evaluation("holdout", holdout_calendar, best_holdout)

    # ---- 7. 上线门槛（基于留出测试段的唯一一次评估）----
    gate = _evaluate_gate(req, best_holdout)

    return OptimizeResult(
        candidate_codes=codes,
        data_start=calendar[0].isoformat(),
        data_end=calendar[-1].isoformat(),
        sample_count=len(calendar),
        splits=_split_info(train_part, validation_part, holdout_part),
        max_trials=req.max_trials,
        total_candidates=total_candidates,
        executed_trials=len(trials),
        trials=trial_summaries,
        best_params=best_combo.to_schema(),
        validation=validation_eval,
        holdout=holdout_eval,
        gate=gate,
        methodology=OPTIMIZE_METHODOLOGY,
        warnings=warnings,
    )
