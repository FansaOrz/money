"""Walk-Forward 滚动窗口组合回测。

流程（全部为只读研究能力，不产生任何实盘下单行为）：
1. 候选池：显式 candidate_codes 或当前持仓基金；装载 FundNav 净值
   （优先累计净值，缺失回退单位净值），对齐到共同交易日交集；
2. 滚动窗口：train_window 个样本训练打分、随后 test_window 个样本样本外
   持有，每 step 个样本向前滚动一次（默认 120/20/20 不重叠滚动）；
3. 打分：仅用训练窗口内（打分基准日及之前）的数据，复用 quant_factors
   的动量/风险调整动量/趋势/回撤因子与横截面 z-score 综合分（因子权重
   可由 FactorWeights 参数化，缺省与规则模型 V1 一致），
   取 top_n 只按综合分归一目标权重（综合分低于 score_threshold 时不入选），
   受单基金 25%、单一市场 50% 约束，截断部分保留为现金；
   不卖空（权重 ∈ [0,1]，合计 ≤ 1）；
4. 测试期：以打分基准日净值建仓，买入并持有至窗口结束；期内不调仓，
   现金零收益、不计手续费与滑点；
5. 基准：全部有效候选基金等权买入持有（B0），同样从首个测试日起算；
6. 输出：策略/基准净值曲线（均匀抽样）、各窗口 segments 明细、
   汇总指标 summary、方法说明 methodology 与数据提示 warnings。

无未来数据保证：第 k 段的目标权重只由训练窗口 [start, start+train_window)
内的净值决定，测试窗口 [start+train_window, ...) 的数据在打分时不可见。

仅使用标准库 + SQLAlchemy；数据源为 FundNav。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from sqlalchemy.orm import Session

from app.schemas.quant import (
    WalkForwardCurvePoint,
    WalkForwardRequest,
    WalkForwardResult,
    WalkForwardSegment,
    WalkForwardSummary,
)
from app.services import quant_factors as factors
from app.services.quant import (
    QuantError,
    _annual_return,
    _daily_returns,
    _load_nav_series,
    _max_drawdown,
    _parse_day,
    _sharpe,
    _win_rate,
)
from app.services.quant_screener import (
    MAX_FUND_WEIGHT,
    MAX_MARKET_WEIGHT,
    _load_candidates,
)

# 读取净值的最大条数（覆盖 500 训练窗口 + 测试滚动 + 冗余）
NAV_LOAD_LIMIT = 5000
MAX_CURVE_POINTS = 260  # 曲线抽样上限（与 quant.py 回测一致）

WALKFORWARD_METHODOLOGY = (
    "Walk-Forward 滚动窗口组合回测：train_window 个净值样本训练、"
    "test_window 个样本样本外测试、每 step 个样本向前滚动（默认 120/20/20）。"
    "每个窗口仅用训练期数据打分：动量 MOM=0.5×R20+0.3×R60+0.2×R120"
    "（窗口不足重新归一化）、60 日风险调整动量、MA20/MA60 趋势 ∈[-1,1]、"
    "120 日最大回撤，横截面 z-score 按 0.45/0.35/0.20/0.50 合成综合分；"
    "选 top_n 只按综合分归一目标权重，受单基金 ≤25%、单一市场 ≤50% 约束，"
    "截断部分保留为现金；不卖空、不计手续费与滑点、现金零收益。"
    "打分基准日收盘建仓，测试期内买入并持有至窗口结束。"
    "基准为全部有效候选基金等权买入持有（B0）。"
    "净值序列对齐到候选共同交易日；净值非正的日期剔除。"
    "年化按 252 个交易日折算，夏普比率采用 2% 无风险利率。"
    "仅为研究回测，不构成投资建议，不产生任何自动交易。"
)


# ---------------------------------------------------------------------------
# 数据装载与对齐
# ---------------------------------------------------------------------------


def _load_aligned_panels(
    db: Session, req: WalkForwardRequest
) -> tuple[list[date], dict[str, list[float]], list[str], list[str]]:
    """装载候选基金净值并对齐到共同交易日。

    返回 (共同交易日升序, {code: 等长净值序列}, 有效候选代码, 警告)。
    对齐前样本不足 train_window+step 的基金被剔除并提示。
    """
    start = _parse_day(req.start_date)
    end = _parse_day(req.end_date)
    if start and end and start > end:
        raise QuantError("start_date 不能晚于 end_date")

    instruments, warnings = _load_candidates(db, req.candidate_codes)

    min_samples = req.window.train_window + req.window.step
    series_by_code: dict[str, list[tuple[date, float]]] = {}
    for instrument in instruments:
        series = _load_nav_series(db, instrument.id, start=start, end=end, limit=NAV_LOAD_LIMIT)
        if len(series) < min_samples:
            warnings.append(
                f"基金 {instrument.code}（{instrument.name}）净值样本不足 "
                f"{min_samples} 条（当前 {len(series)} 条），已从候选池剔除"
            )
            continue
        series_by_code[instrument.code] = series

    if len(series_by_code) < 2:
        raise QuantError(
            f"有效候选基金不足 2 只（{len(series_by_code)} 只满足样本要求），"
            "无法构造等权基准与横截面打分，请扩大候选池或缩短窗口"
        )

    # 共同交易日交集：保证策略与基准逐日可比
    common: set[date] | None = None
    for series in series_by_code.values():
        days = {d for d, _ in series}
        common = days if common is None else (common & days)
    calendar = sorted(common or set())
    if len(calendar) < min_samples:
        raise QuantError(
            f"候选基金共同交易日仅 {len(calendar)} 天，不足训练窗口+一个滚动步长 "
            f"（{min_samples} 天），请缩短窗口或核对净值区间"
        )

    panels: dict[str, list[float]] = {}
    for code, series in series_by_code.items():
        values = dict(series)
        panels[code] = [values[d] for d in calendar]

    if any(len(s) != len(calendar) for s in series_by_code.values()):
        warnings.append(
            f"各基金净值日期不完全一致，已对齐到共同交易日交集（{len(calendar)} 天）"
        )

    codes = [instrument.code for instrument in instruments if instrument.code in panels]
    return calendar, panels, codes, warnings


# ---------------------------------------------------------------------------
# 打分与目标权重（仅使用训练窗口数据）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FactorWeights:
    """综合分的因子权重（参数化入口，供规则参数优化搜索）。

    缺省值与规则模型 V1 完全一致；权重之和不要求为 1（综合分为加权和，
    横截面比较只看相对大小）。全部为 0 时所有候选同分（0 分）。
    """

    momentum: float = factors.SCORE_WEIGHT_MOMENTUM
    risk_adjusted: float = factors.SCORE_WEIGHT_RISK_ADJ
    trend: float = factors.SCORE_WEIGHT_TREND
    drawdown: float = factors.SCORE_WEIGHT_DRAWDOWN

    def as_dict(self) -> dict[str, float]:
        return {
            "momentum": self.momentum,
            "risk_adjusted": self.risk_adjusted,
            "trend": self.trend,
            "drawdown": self.drawdown,
        }


DEFAULT_FACTOR_WEIGHTS = FactorWeights()


def _score_candidates(
    train_values: dict[str, list[float]],
    weights: FactorWeights | None = None,
) -> dict[str, float]:
    """用训练窗口净值对候选池横截面打分，返回 {code: 综合分}。

    综合分 = w_mom×z(MOM) + w_ram×z(RAM60) + w_trend×TREND + w_dd×z(DRAWDOWN)，
    缺失项按 0 处理；weights 缺省时与规则模型 V1 一致。
    """
    weights = weights or DEFAULT_FACTOR_WEIGHTS
    momentum: dict[str, float | None] = {}
    risk_adjusted: dict[str, float | None] = {}
    trend: dict[str, float | None] = {}
    drawdown: dict[str, float | None] = {}
    for code, values in train_values.items():
        momentum[code] = factors.momentum_score(values)[0]
        risk_adjusted[code] = factors.risk_adjusted_momentum(values)
        trend[code] = factors.trend_strength(values)[0]
        drawdown[code] = factors.max_drawdown(values, window=factors.DRAWDOWN_WINDOW)

    momentum_z = factors.zscores(momentum)
    risk_adj_z = factors.zscores(risk_adjusted)
    drawdown_z = factors.zscores(drawdown)
    return {
        code: (
            weights.momentum * (momentum_z.get(code) or 0.0)
            + weights.risk_adjusted * (risk_adj_z.get(code) or 0.0)
            + weights.trend * (trend.get(code) or 0.0)
            + weights.drawdown * (drawdown_z.get(code) or 0.0)
        )
        for code in train_values
    }


def _target_weights(
    scores: dict[str, float],
    markets: dict[str, str],
    top_n: int,
    score_threshold: float | None = None,
) -> dict[str, float]:
    """选择 top_n 并在基金/市场上限下分配；无法分配部分留现金。"""
    ordered = sorted(scores, key=lambda code: scores[code], reverse=True)
    if score_threshold is not None:
        ordered = [code for code in ordered if scores[code] >= score_threshold]
    ranked = ordered[: max(top_n, 1)]
    if not ranked:
        return {}

    positive = {code: max(scores[code], 0.0) for code in ranked}
    if sum(positive.values()) <= 0:
        positive = dict.fromkeys(ranked, 1.0)
    total = sum(positive.values())
    desired = {code: value / total for code, value in positive.items()}

    weights: dict[str, float] = {}
    market_used: dict[str, float] = {}
    for code in ranked:
        market = markets.get(code, "cn")
        room = max(MAX_MARKET_WEIGHT - market_used.get(market, 0.0), 0.0)
        weight = min(desired[code], MAX_FUND_WEIGHT, room)
        if weight > 0:
            weights[code] = weight
            market_used[market] = market_used.get(market, 0.0) + weight

    total_weight = sum(weights.values())
    if total_weight > 1.0:
        weights = {code: weight / total_weight for code, weight in weights.items()}
    return {code: round(weight, 6) for code, weight in weights.items() if weight > 0}


# ---------------------------------------------------------------------------
# 指标汇总
# ---------------------------------------------------------------------------


def _summarize(values: list[float]) -> WalkForwardSummary:
    """由净值序列（起点 1.0）汇总各项指标。"""
    if len(values) < 2:
        return WalkForwardSummary()
    returns = _daily_returns(values)
    total_return = values[-1] - 1.0
    return WalkForwardSummary(
        total_return=total_return,
        annual_return=_annual_return(total_return, len(values) - 1),
        max_drawdown=_max_drawdown(values),
        sharpe=_sharpe(returns),
        win_rate=_win_rate(returns),
    )


def _sample_curve(
    calendar: list[date], strategy: list[float], benchmark: list[float]
) -> list[WalkForwardCurvePoint]:
    """控制响应规模：超过上限时均匀抽样（策略与基准同日期对齐）。"""
    n = len(calendar)
    if n <= MAX_CURVE_POINTS:
        indices = range(n)
    else:
        step = n / MAX_CURVE_POINTS
        indices = sorted({int(i * step) for i in range(MAX_CURVE_POINTS)} | {n - 1})
    return [
        WalkForwardCurvePoint(
            date=calendar[i].isoformat(),
            strategy=round(strategy[i], 6),
            benchmark=round(benchmark[i], 6),
        )
        for i in indices
    ]


# ---------------------------------------------------------------------------
# 回测引擎（纯函数，便于单测无未来数据）
# ---------------------------------------------------------------------------


def run_walkforward_panels(
    calendar: list[date],
    panels: dict[str, list[float]],
    markets: dict[str, str],
    req: WalkForwardRequest,
    factor_weights: FactorWeights | None = None,
    score_threshold: float | None = None,
    embargo: int = 0,
    rebalance_interval: int | None = None,
) -> tuple[
    list[float],
    list[float],
    list[WalkForwardSegment],
    float,
    list[str],
]:
    """在对齐的净值面板上执行 Walk-Forward 回测（不访问数据库）。

    窗口滚动：start 从 0 起，每次 +step（要求 step ≥ test_window，样本外
    测试区间互不重叠，schema 已校验）；训练窗口 [start, start+train)，
     embargo 个样本的隔离带（purged walk-forward，默认 0 保持原行为），
    测试窗口 [start+train+embargo, min(start+train+embargo+test, n))。
    打分只使用训练窗口内的数据（无未来数据）。
    factor_weights / score_threshold 为可选的因子参数化（缺省与规则模型
    V1 一致、不设入选阈值），rebalance_interval 为调仓间隔（每隔多少个
    测试窗口重新打分调仓一次，缺省为 1 即每个测试窗口都调仓），
    均供规则参数优化在网格搜索时传入。

    记账方式：全程逐日份额估值 —— 策略净值为各基金持有份额 × 当日净值
    之和 + 现金；调仓日（打分基准日收盘）按当日净值把全部持仓折算为
    现金等价物后按新目标权重重新买入。上一段的涨跌只通过组合净值结转，
    不会因为重新锚定成本而重复累计。

    返回 (策略净值序列, 基准净值序列, 窗口明细, 平均换手率, 警告)。
    净值序列与 calendar[首个测试日起:] 逐日一一对齐，起点均为 1.0，
    两者长度一致（len(strategy) == len(benchmark)）。
    """
    train = req.window.train_window
    test = req.window.test_window
    step = req.window.step
    embargo = max(embargo, 0)
    rebalance_interval = max(1, rebalance_interval or 1)
    n = len(calendar)
    if step < test:
        # schema 已校验；此处兜底防御未经 schema 构造的请求，
        # 重叠的样本外测试区间会把同一交易日的收益重复计入净值曲线
        raise QuantError(
            f"step（{step}）必须 ≥ test_window（{test}）："
            "更小的步长会使样本外测试区间重叠、收益被重复累计"
        )
    codes = list(panels)
    warnings: list[str] = []

    # 窗口起点：首段测试从 train+embargo 开始；尾段不足一个完整 test_window
    # 时，若剩余样本 >= test//2 则并入最后一段（避免丢弃尾部样本外区间）
    starts: list[int] = []
    cursor = 0
    while cursor + train + embargo + test <= n:
        starts.append(cursor)
        cursor += step
    tail_start = len(starts) * step
    if (
        starts
        and tail_start + train + embargo < n
        and n - (tail_start + train + embargo) >= max(test // 2, 1)
    ):
        starts.append(tail_start)
    if not starts:
        raise QuantError(
            f"净值样本 {n} 天不足以构造一个完整窗口"
            f"（需要 train_window+embargo+test_window = {train + embargo + test} 天），请缩短窗口"
        )

    # ---- 逐日份额估值状态：净值曲线与 calendar[首个测试日起:] 一一对齐 ----
    # 调仓日（打分基准日收盘）以当日净值把组合折算后按新目标权重重建份额；
    # 非调仓日份额与现金原样延续（自然漂移），跨段连续、不重新锚定，
    # 因而任何一段收益都不会被重复累计。
    shares: dict[str, float] = {}
    cash = 1.0  # 组合初始净值 1.0 全部为现金
    strategy: list[float] = []
    segments: list[WalkForwardSegment] = []
    turnovers: list[float] = []
    seg_start_equity = 1.0

    for index, start in enumerate(starts, start=1):
        train_end_idx = start + train - 1  # 打分基准日（训练窗口最后一个样本）
        test_begin = start + train + embargo
        test_end = min(start + train + embargo + test, n)  # 左闭右开

        # ---- 调仓节奏：每隔 rebalance_interval 个测试窗口重新打分调仓一次；
        # 非调仓期沿用既有份额（自然漂移），无换手、不产生新的打分；
        # 上一调仓期未入选任何基金（全现金）时，本期重新打分 ----
        is_rebalance = (index - 1) % rebalance_interval == 0 or (not shares and cash > 0.0)

        if is_rebalance:
            # ---- 打分：仅训练窗口 [start, start+train) 内的数据 ----
            train_values = {code: panels[code][start : start + train] for code in codes}
            scores = _score_candidates(train_values, factor_weights)
            target = _target_weights(scores, markets, req.top_n, score_threshold)
            if not target:
                warnings.append(f"第 {index} 期无有效入选基金，本期持有现金（零收益）")

            # ---- 调仓：每个样本外测试段在 test_begin 建仓。训练窗口可能滚动重叠，
            # train_end_idx 会早于上一测试段末日；若用该历史净值给现有份额估值，
            # 会把组合“倒回过去”并重复制造收益。调仓估值/成交统一使用 test_begin。
            nav_close = {code: panels[code][test_begin] for code in codes}
            equity_before_rebalance = (
                sum(shares[code] * nav_close[code] for code in shares) + cash
            )
            # 该测试段收益从本段实际建仓/重平衡后的净值开始计算；测试首日
            # 之前的隔夜漂移属于前一段或未投资空档，不归入本段策略收益。
            seg_start_equity = equity_before_rebalance
            equity_at_close = equity_before_rebalance
            # ---- 换手率：Σ|目标权重 - 漂移权重| / 2（首个调仓期漂移权重全 0）----
            # 漂移权重 = 当前持仓按调仓日收盘净值折算后的市值占比（含现金），
            # 现金与基金一并归一化后比较；现金项目标与漂移一致，自然抵消
            drifted: dict[str, float] = {}
            if equity_at_close > 0:
                drifted = {
                    code: amount * nav_close[code] / equity_at_close
                    for code, amount in shares.items()
                }
            target_with_cash = dict(target)
            drifted_with_cash = dict(drifted)
            target_with_cash["__cash__"] = max(1.0 - sum(target.values()), 0.0)
            drifted_with_cash["__cash__"] = max(1.0 - sum(drifted.values()), 0.0)
            turnover = 0.5 * sum(
                abs(target_with_cash.get(k, 0.0) - drifted_with_cash.get(k, 0.0))
                for k in set(target_with_cash) | set(drifted_with_cash)
            )
            turnovers.append(turnover)

            shares = {
                code: target[code] * equity_at_close / nav_close[code]
                for code in target
            }
            cash = target_with_cash["__cash__"] * equity_at_close
        else:
            # 非调仓期：份额与现金原样延续，明细仍展示上一调仓期的目标权重
            target = dict(segments[-1].holdings) if segments else {}

        # ---- 测试期：逐日份额估值（现金零收益，但仍计入组合价值）----
        for t in range(test_begin, test_end):
            equity = sum(amount * panels[code][t] for code, amount in shares.items()) + cash
            strategy.append(equity)
        seg_end_equity = strategy[-1]

        # ---- 窗口明细（基准段收益待基准曲线生成后统一回填）----
        seg_ret = (seg_end_equity / seg_start_equity - 1.0) if seg_start_equity > 0 else None
        seg_start_equity = seg_end_equity
        segments.append(
            WalkForwardSegment(
                index=index,
                train_start=calendar[start].isoformat(),
                train_end=calendar[train_end_idx].isoformat(),
                test_start=calendar[test_begin].isoformat(),
                test_end=calendar[test_end - 1].isoformat(),
                holdings=target,
                segment_return=seg_ret,
                benchmark_return=None,
            )
        )

    # 基准：全部候选等权买入持有（B0）。基准仅保留真正的样本外测试日，
    # 当 step > test_window 时跳过窗口间空档，确保与策略长度、日期逐点一致。
    bench_start = starts[0] + train + embargo
    bench_base = {code: panels[code][bench_start] for code in codes}
    benchmark_full: dict[int, float] = {}
    for t in range(bench_start, n):
        benchmark_full[t] = sum(
            panels[code][t] / bench_base[code] for code in codes
        ) / len(codes)
    test_indices: list[int] = []
    for index, start in enumerate(starts):
        test_begin = start + train + embargo
        test_end = min(start + train + embargo + test, n)
        indices = list(range(test_begin, test_end))
        test_indices.extend(indices)
        bench_seg = [benchmark_full[t] for t in indices]
        bench_ret = (
            (bench_seg[-1] / bench_seg[0] - 1.0)
            if len(bench_seg) >= 2 and bench_seg[0] > 0
            else None
        )
        segments[index] = segments[index].model_copy(update={"benchmark_return": bench_ret})
    benchmark = [benchmark_full[t] for t in test_indices]

    # 平均换手率只计真正的调仓期：只有 1 个调仓期时就是首期建仓（漂移权重
    # 全 0 的机械值，不构成调仓），平均换手为 0
    rebalance_turnovers = turnovers[1:]
    avg_turnover = (
        sum(rebalance_turnovers) / len(rebalance_turnovers) if rebalance_turnovers else 0.0
    )
    return strategy, benchmark, segments, avg_turnover, warnings


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------


def run_walkforward(db: Session, req: WalkForwardRequest) -> WalkForwardResult:
    """Walk-Forward 组合回测入口：装载对齐 → 滚动回测 → 汇总输出。"""
    calendar, panels, codes, warnings = _load_aligned_panels(db, req)

    # 市场分类（用于单一市场 50% 约束）需要基金名称
    from sqlalchemy import select

    from app.models import Instrument

    markets = {code: "cn" for code in codes}
    rows = db.execute(
        select(Instrument.code, Instrument.name).where(Instrument.code.in_(codes))
    ).all()
    for code, name in rows:
        markets[code] = factors.classify_market(name)

    strategy, benchmark, segments, avg_turnover, run_warnings = run_walkforward_panels(
        calendar, panels, markets, req
    )
    warnings.extend(run_warnings)

    strategy_summary = _summarize(strategy)
    benchmark_summary = _summarize(benchmark)
    excess = (
        strategy_summary.total_return - benchmark_summary.total_return
        if strategy_summary.total_return is not None
        and benchmark_summary.total_return is not None
        else None
    )

    # 曲线日期使用每个 segment 的真实测试日，不能用尾部切片猜测；
    # step > test_window 时，中间空档不属于样本外结果。
    date_by_text = {day.isoformat(): day for day in calendar}
    curve_calendar: list[date] = []
    for segment in segments:
        start_day = date_by_text[segment.test_start]
        end_day = date_by_text[segment.test_end]
        curve_calendar.extend(day for day in calendar if start_day <= day <= end_day)
    params: dict[str, float | int | str | list[str]] = {
        "train_window": req.window.train_window,
        "test_window": req.window.test_window,
        "step": req.window.step,
        "top_n": req.top_n,
        "candidate_codes": codes,
    }

    return WalkForwardResult(
        params=params,
        start_date=curve_calendar[0].isoformat(),
        end_date=curve_calendar[-1].isoformat(),
        initial_capital=req.initial_capital,
        strategy=strategy_summary,
        benchmark=benchmark_summary,
        excess_return=excess,
        turnover=round(avg_turnover, 6),
        rebalance_count=len(segments),
        curve=_sample_curve(curve_calendar, strategy, benchmark),
        segments=segments,
        methodology=WALKFORWARD_METHODOLOGY,
        warnings=warnings,
    )
