"""量化验证服务：as_of 快照下的样本外验证与稳健性检验。

编排流程（全部为只读研究能力，不产生任何实盘下单行为）：
1. 候选池与快照：显式 candidate_codes 或当前持仓基金；as_of 指定时按
   QDII lag2 / 国内 lag1 折算各基金可用净值截止日（quant_snapshot），
   仅使用该日及之前的数据；
2. 样本外回测：复用 walk-forward 滚动窗口（quant_walkforward），
   打分只用训练窗口数据（无未来数据）；include_costs 时在调仓日按
   费用模型（quant_costs：买 0.15%、卖默认 0.5%/7 日内 1.5%，基于
   lot 持有期 FIFO 估算）扣除交易费用；
3. 指标（quant_stats 纯函数）：
   - 风险：CVaR95、Calmar、信息比率（基准 = 候选等权买入持有 B0）；
   - 预测有效性：各调仓期 Rank IC（Spearman）均值、五档收益单调性
     （按分数分五档的平均前瞻收益 + Kendall tau）；
   - 稳健性：Deflated Sharpe 简化实现（记录 trial_count / skew /
     kurtosis）、block bootstrap White Reality Check 近似；
   - 参数邻域稳定性：top_n 与调仓间隔 ±1、因子权重 ±0.05 扰动的
     邻域样本外夏普分位数与 ±1 档稳定性带。
"""

from __future__ import annotations

from datetime import date
from statistics import fmean

from sqlalchemy.orm import Session

from app.schemas.quant import (
    SnapshotFundInfo,
    SnapshotResponse,
    ValidationCostSummary,
    ValidationFundSnapshot,
    ValidationNeighborhood,
    ValidationPredictiveness,
    ValidationRequest,
    ValidationResponse,
    ValidationRiskMetrics,
    ValidationRobustness,
    WalkForwardRequest,
    WalkForwardWindow,
)
from app.services import quant_costs as costs
from app.services import quant_snapshot as snapshot
from app.services import quant_stats as stats
from app.services import quant_factors as factors
from app.services.quant import QuantError, _parse_day
from app.services.quant_screener import _load_candidates
from app.services.quant_walkforward import (
    DEFAULT_FACTOR_WEIGHTS,
    FactorWeights,
    _score_candidates,
    run_walkforward_panels,
)

MAX_SNAPSHOT_DAYS = 500  # 快照接口返回的交易日列表规模上限（保留尾部）

VALIDATION_METHODOLOGY = (
    "量化验证：as_of 快照（QDII lag2 / 国内 lag1，仅用当时可见净值）下的 "
    "walk-forward 滚动窗口样本外验证。打分仅用训练窗口数据（动量/风险调整动量/"
    "趋势/回撤横截面 z-score 综合分），选 top_n 只受约束目标权重建仓，测试期"
    "买入并持有；费用模型买 0.15%、卖默认 0.5%（持有 <7 自然日 1.5%，基于 lot "
    "FIFO 持有期估算，无流水时按默认费率）。风险指标：CVaR95（最差 5% 日收益"
    "均值）、Calmar（年化/|最大回撤|）、信息比率（主动收益/跟踪误差，基准为"
    "候选等权 B0）。预测有效性：各调仓期 Rank IC（Spearman）均值与五档收益"
    "单调性（Kendall tau）。稳健性：Deflated Sharpe 简化实现（Bailey-López de "
    "Prado 期望最大夏普近似，记录 trial_count/skew/kurtosis）、block bootstrap "
    "White Reality Check 近似（主动收益去均值后循环块重抽样）。参数邻域稳定性："
    "top_n 与调仓间隔 ±1、因子权重 ±0.05 扰动的邻域样本外夏普分位数与 ±1 档带。"
    "幸存者偏差声明：验证基于当前候选池（当前持仓或当前指定成员），历史时点"
    "已清盘/调出池的基金不在样本内，样本外指标可能系统性偏好存活至今的基金。"
    "仅为研究验证，不构成投资建议，不产生任何自动交易。"
)


# ---------------------------------------------------------------------------
# 日收益序列构造与费用扣除
# ---------------------------------------------------------------------------


def _daily_returns_from_curve(values: list[float]) -> list[float]:
    return [values[i] / values[i - 1] - 1.0 for i in range(1, len(values)) if values[i - 1] > 0]


def _apply_turnover_costs(
    oos_returns: list[float],
    calendar: list[date],
    oos_start_index: int,
    turnovers: list[tuple[int, dict[str, float], dict[str, float]]],
    lots_by_code: dict[str, list[costs.ShareLot]],
    cost_model,
) -> tuple[list[float], float, int, str]:
    """在样本外日收益上按调仓换手扣除费用。

    turnovers 为 (调仓日下标, 上期目标权重, 本期目标权重)；买入比例与
    卖出比例均按组合总价值口径（单边 Σ|Δw| 的买卖分解）。卖出费用按
    lot 持有期估算；无 lot 数据时按默认费率。费用在调仓次日（首个
    样本外收益日）的收益中扣除。

    返回 (扣费后的日收益, 累计费用比例, 交易天数, 卖出费率依据)。
    """
    result = list(oos_returns)
    total_fee = 0.0
    trade_days = 0
    basis = "lots" if any(lots_by_code.values()) else "default"

    for anchor_index, prev_weights, target in turnovers:
        day = calendar[anchor_index]
        codes = set(prev_weights) | set(target)
        buy_ratio = 0.0
        sell_fee = 0.0
        for code in codes:
            delta = target.get(code, 0.0) - prev_weights.get(code, 0.0)
            if delta > 0:
                buy_ratio += delta
            elif delta < 0:
                sell_ratio = -delta
                lots = lots_by_code.get(code) or []
                if lots:
                    rate, _ = costs.estimate_sell_fee(
                        lots,
                        shares=sell_ratio,  # 比例口径：lot 份额占比同样适用加权费率
                        sell_date=day,
                        default_rate=cost_model.sell_fee_rate,
                        short_term_rate=cost_model.short_term_sell_fee_rate,
                        short_term_days=cost_model.short_term_days,
                    )
                else:
                    rate = cost_model.sell_fee_rate
                sell_fee += sell_ratio * rate
        fee = buy_ratio * cost_model.buy_fee_rate + sell_fee
        if fee <= 0:
            continue
        # 调仓以 anchor 日净值成交，费用体现在次日收益上
        ret_index = anchor_index - oos_start_index
        if 0 <= ret_index < len(result):
            result[ret_index] = (1.0 + result[ret_index]) * (1.0 - fee) - 1.0
            total_fee += fee
            trade_days += 1
    return result, total_fee, trade_days, basis


# ---------------------------------------------------------------------------
# 预测有效性：Rank IC 与五档单调性
# ---------------------------------------------------------------------------


def _predictiveness(
    calendar: list[date],
    panels: dict[str, list[float]],
    req: ValidationRequest,
) -> tuple[ValidationPredictiveness, list[str]]:
    """各调仓期综合分 vs 测试期前瞻收益的 Rank IC 与五档单调性。"""
    warnings: list[str] = []
    train = req.window.train_window
    test = req.window.test_window
    step = req.window.step
    interval = max(req.rebalance_interval, 1)
    n = len(calendar)
    codes = list(panels)

    starts: list[int] = []
    cursor = 0
    while cursor + train + test <= n:
        starts.append(cursor)
        cursor += step

    rank_ics: list[float] = []
    all_scores: list[float] = []
    all_forward: list[float] = []
    for index, start in enumerate(starts, start=1):
        if (index - 1) % interval != 0:
            continue  # 非调仓期沿用旧持仓，无新打分
        train_values = {code: panels[code][start : start + train] for code in codes}
        scores = _score_candidates(train_values)
        score_date = calendar[start + train - 1]
        forward: dict[str, float] = {}
        for code in codes:
            base = panels[code][start + train - 1]
            end_value = panels[code][min(start + train + test, n) - 1]
            if base > 0:
                forward[code] = end_value / base - 1.0
        valid = [code for code in codes if code in forward]
        if len(valid) < 3:
            continue
        score_list = [scores[code] for code in valid]
        forward_list = [forward[code] for code in valid]
        if len(set(forward_list)) < 2:
            warnings.append(
                f"打分日 {score_date.isoformat()} 的前瞻收益全部相同，该期 Rank IC 跳过"
            )
            continue
        ic = stats.rank_ic(score_list, forward_list)
        if ic is None and len(valid) >= 2:
            # 两只基金时 Spearman 排名仍有明确方向，直接用二元排序一致性。
            score_diff = score_list[0] - score_list[1]
            return_diff = forward_list[0] - forward_list[1]
            if score_diff != 0 and return_diff != 0:
                ic = 1.0 if score_diff * return_diff > 0 else -1.0
        if ic is not None:
            rank_ics.append(ic)
            all_scores.extend(score_list)
            all_forward.extend(forward_list)

    quintile = stats.quintile_monotonicity(all_scores, all_forward)
    return ValidationPredictiveness(
        rank_ic_mean=fmean(rank_ics) if rank_ics else None,
        rank_ic_count=len(rank_ics),
        quintile_returns=list(quintile.quintile_returns) if quintile else [],
        quintile_spread=quintile.spread if quintile else None,
        quintile_kendall_tau=quintile.kendall_tau if quintile else None,
        quintile_monotonic=quintile.monotonic if quintile else False,
    ), warnings


# ---------------------------------------------------------------------------
# 参数邻域稳定性
# ---------------------------------------------------------------------------


def _neighborhood(
    calendar: list[date],
    panels: dict[str, list[float]],
    markets: dict[str, str],
    req: ValidationRequest,
) -> ValidationNeighborhood:
    """邻域参数点的样本外夏普：top_n ±1、调仓间隔 ±1、因子权重 ±0.05 扰动。"""
    window = WalkForwardWindow(
        train_window=req.window.train_window,
        test_window=req.window.test_window,
        step=req.window.step,
    )
    candidates = max(len(panels), 1)

    variants: dict[str, tuple[int, int, FactorWeights | None]] = {}
    for top_n in sorted({req.top_n - 1, req.top_n + 1}):
        if 1 <= top_n <= min(20, candidates):
            variants[f"top_n={top_n}"] = (top_n, req.rebalance_interval, None)
    for interval in sorted({req.rebalance_interval - 1, req.rebalance_interval + 1}):
        if 1 <= interval <= 60:
            variants[f"rebalance_interval={interval}"] = (req.top_n, interval, None)
    for dim, base in DEFAULT_FACTOR_WEIGHTS.as_dict().items():
        for step in (-1, 1):
            value = round(base + 0.05 * step, 4)
            if value < 0:
                continue
            weights = FactorWeights(
                **{**DEFAULT_FACTOR_WEIGHTS.as_dict(), dim: value}
            )
            variants[f"w_{dim}={value}"] = (req.top_n, req.rebalance_interval, weights)

    center_sharpe: float | None = None
    neighbor_sharpes: list[float] = []
    neighbors: dict[str, float | None] = {}

    def _run(top_n: int, interval: int, weights: FactorWeights | None) -> float | None:
        wf_req = WalkForwardRequest(
            candidate_codes=list(panels), window=window, top_n=top_n,
        )
        try:
            strategy, _bench, _segments, _turnover, _warnings = run_walkforward_panels(
                calendar, panels, markets, wf_req,
                factor_weights=weights,
                rebalance_interval=interval,
            )
        except QuantError:
            return None
        return stats.sharpe_ratio(_daily_returns_from_curve(strategy))

    center_sharpe = _run(req.top_n, req.rebalance_interval, None)
    for label, (top_n, interval, weights) in variants.items():
        sharpe = _run(top_n, interval, weights)
        neighbors[label] = sharpe
        if sharpe is not None:
            neighbor_sharpes.append(sharpe)

    if center_sharpe is None or not neighbor_sharpes:
        return ValidationNeighborhood(
            center_sharpe=center_sharpe,
            neighbors=neighbors,
            neighbor_count=(1 if center_sharpe is not None else 0) + len(neighbor_sharpes),
        )
    stability = stats.neighborhood_stability(center_sharpe, neighbor_sharpes)
    return ValidationNeighborhood(
        center_sharpe=center_sharpe,
        neighborhood_quantile=stability.neighborhood_quantile if stability else None,
        band_low=stability.band_low if stability else None,
        band_high=stability.band_high if stability else None,
        neighbor_count=stability.neighbor_count if stability else 0,
        neighbors=neighbors,
    )


# ---------------------------------------------------------------------------
# 风险指标汇总
# ---------------------------------------------------------------------------


def _risk_metrics(curve: list[float]) -> ValidationRiskMetrics:
    """由净值曲线（起点 1.0）汇总风险指标。"""
    if len(curve) < 2:
        return ValidationRiskMetrics()
    returns = _daily_returns_from_curve(curve)
    total = curve[-1] / curve[0] - 1.0 if curve[0] > 0 else None
    max_dd = stats.max_drawdown(curve)
    return ValidationRiskMetrics(
        total_return=total,
        annual_return=stats.annualized_return(total, len(curve) - 1) if total is not None else None,
        sharpe=stats.sharpe_ratio(returns),
        max_drawdown=max_dd,
        cvar95=stats.cvar95(returns),
        calmar=stats.calmar_ratio(total, len(curve) - 1, max_dd) if total is not None else None,
        win_rate=(sum(1 for r in returns if r > 0) / len(returns)) if returns else None,
    )


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------


def run_validation(db: Session, req: ValidationRequest) -> ValidationResponse:
    """量化验证入口：快照装载 → 样本外回测 → 指标汇总。"""
    as_of = _parse_day(req.as_of)
    instruments, warnings = _load_candidates(db, req.candidate_codes)

    min_samples = req.window.train_window + req.window.test_window
    calendar, panels, fund_snapshots, snap_warnings = snapshot.load_snapshot_panels(
        db, instruments, as_of, min_samples=min_samples
    )
    warnings.extend(snap_warnings)

    # 市场分类（单一市场 50% 约束用）
    markets = {code: "cn" for code in panels}
    name_by_code = {instrument.code: instrument.name for instrument in instruments}
    for code in panels:
        markets[code] = factors.classify_market(name_by_code.get(code, ""))

    codes = list(panels)
    wf_req = WalkForwardRequest(
        candidate_codes=codes,
        window=WalkForwardWindow(
            train_window=req.window.train_window,
            test_window=req.window.test_window,
            step=req.window.step,
        ),
        top_n=req.top_n,
    )
    strategy, benchmark, segments, _avg_turnover, run_warnings = run_walkforward_panels(
        calendar, panels, markets, wf_req, rebalance_interval=req.rebalance_interval
    )
    warnings.extend(run_warnings)

    oos_start = len(calendar) - len(strategy)
    oos_dates = calendar[oos_start:]
    strategy_returns = _daily_returns_from_curve(strategy)
    benchmark_returns = _daily_returns_from_curve(benchmark)

    # ---- 费用 ----
    lots_by_code: dict[str, list[costs.ShareLot]] = {}
    sell_basis = "default"
    total_fee = 0.0
    trade_days = 0
    if req.include_costs:
        id_by_code = {instrument.code: instrument.id for instrument in instruments}
        lots_by_code = {
            code: costs.load_open_lots(db, id_by_code[code])
            for code in codes
            if code in id_by_code
        }
        if not any(lots_by_code.values()):
            warnings.append(
                "候选基金无交易流水，卖出费用按默认费率估算（无法确定 lot 持有期）"
            )
        # 调仓换手：(anchor 下标, 上期权重, 本期权重)
        date_to_index = {day: i for i, day in enumerate(calendar)}
        turnovers: list[tuple[int, dict[str, float], dict[str, float]]] = []
        prev_weights: dict[str, float] = {}
        for segment in segments:
            train_end = _parse_day(segment.train_end)
            if train_end is None or train_end not in date_to_index:
                continue
            # 费用落在该段首个样本外收益日；train_end 本身位于 OOS 起点之前，
            # 直接使用它会得到负下标并漏扣首次建仓费用。
            test_start = _parse_day(segment.test_start)
            anchor = date_to_index[test_start] if test_start in date_to_index else date_to_index[train_end]
            if segment.index == 1 or (segment.index - 1) % req.rebalance_interval == 0:
                turnovers.append((anchor, prev_weights, dict(segment.holdings)))
                prev_weights = dict(segment.holdings)
        strategy_returns, total_fee, trade_days, sell_basis = _apply_turnover_costs(
            strategy_returns, calendar, oos_start, turnovers, lots_by_code, req.cost_model
        )
        # 扣费后重建净值曲线用于回撤/Calmar
        curve = [strategy[0]]
        for r in strategy_returns:
            curve.append(curve[-1] * (1.0 + r))
        strategy = curve

    # ---- 风险指标 ----
    strategy_metrics = _risk_metrics(strategy)
    benchmark_metrics = _risk_metrics(benchmark)
    ir = stats.information_ratio(strategy_returns, benchmark_returns)
    excess = (
        strategy_metrics.total_return - benchmark_metrics.total_return
        if strategy_metrics.total_return is not None
        and benchmark_metrics.total_return is not None
        else None
    )

    # ---- 预测有效性 ----
    predictiveness, pred_warnings = _predictiveness(calendar, panels, req)
    warnings.extend(pred_warnings)

    # ---- 稳健性：DSR + White Reality Check ----
    dsr = stats.deflated_sharpe(strategy_returns, req.trial_count)
    reality = stats.white_reality_check(
        strategy_returns,
        benchmark_returns,
        resamples=req.bootstrap_resamples,
        block_length=req.block_length,
        seed=req.seed,
    )
    robustness = ValidationRobustness(
        trial_count=dsr.trial_count if dsr else req.trial_count,
        skew=dsr.skew if dsr else stats.skewness(strategy_returns),
        kurtosis=dsr.kurtosis if dsr else stats.kurtosis(strategy_returns),
        sharpe_std=dsr.sr_std if dsr else None,
        expected_max_sharpe=dsr.expected_max_sr if dsr else None,
        deflated_sharpe=dsr.dsr if dsr else None,
        reality_check_p=reality.p_value if reality else None,
        reality_check_stat=reality.observed_stat if reality else None,
        reality_check_null_mean=reality.null_mean if reality else None,
        bootstrap_resamples=reality.resamples if reality else 0,
        block_length=reality.block_length if reality else (req.block_length or 0),
    )
    if dsr is None:
        warnings.append("样本外收益无法估计夏普标准误（样本不足或零波动），DSR 不可用")
    if reality is None:
        warnings.append("主动收益统计量无法估计（样本不足或零波动），Reality Check 不可用")

    # ---- 参数邻域稳定性 ----
    neighborhood = _neighborhood(calendar, panels, markets, req)

    effective_as_of = as_of.isoformat() if as_of else calendar[-1].isoformat()
    return ValidationResponse(
        as_of=effective_as_of,
        candidate_codes=codes,
        start_date=oos_dates[0].isoformat(),
        end_date=oos_dates[-1].isoformat(),
        sample_count=len(calendar),
        oos_count=len(strategy),
        strategy=strategy_metrics,
        benchmark=benchmark_metrics,
        information_ratio=ir,
        excess_return=excess,
        predictiveness=predictiveness,
        robustness=robustness,
        neighborhood=neighborhood,
        costs=ValidationCostSummary(
            include_costs=req.include_costs,
            buy_fee_rate=req.cost_model.buy_fee_rate,
            sell_fee_rate=req.cost_model.sell_fee_rate,
            short_term_sell_fee_rate=req.cost_model.short_term_sell_fee_rate,
            short_term_days=req.cost_model.short_term_days,
            total_fee_ratio=round(total_fee, 8),
            trade_days=trade_days,
            sell_fee_basis=sell_basis,
        ),
        fund_snapshots=[
            ValidationFundSnapshot(
                code=snap.code,
                name=snap.name,
                is_qdii=snap.is_qdii,
                lag_days=snap.lag_days,
                latest_nav_date=snap.latest_nav_date,
                effective_date=snap.effective_date,
            )
            for snap in fund_snapshots
        ],
        methodology=VALIDATION_METHODOLOGY,
        warnings=warnings,
    )


def get_snapshot(db: Session, codes: list[str] | None, as_of: str | None) -> SnapshotResponse:
    """as_of 可用日期快照：可用交易日与各基金按 lag 折算的有效数据日。"""
    parsed_as_of = _parse_day(as_of)
    instruments, warnings = _load_candidates(db, codes)
    union_days, per_fund = snapshot.list_available_days(db, instruments)
    if not union_days:
        raise QuantError("候选基金均无净值数据，无法构造快照")

    effective_as_of = parsed_as_of or union_days[-1]
    truncated = len(union_days) > MAX_SNAPSHOT_DAYS
    shown_days = union_days[-MAX_SNAPSHOT_DAYS:] if truncated else union_days

    funds: list[SnapshotFundInfo] = []
    for instrument in instruments:
        nav_days = per_fund.get(instrument.code, [])
        lag = snapshot.default_lag_days(instrument.name)
        if parsed_as_of is None:
            effective = nav_days[-1] if nav_days else None
        else:
            effective = snapshot.effective_nav_date(nav_days, parsed_as_of, lag)
        funds.append(
            SnapshotFundInfo(
                code=instrument.code,
                name=instrument.name,
                is_qdii=snapshot.is_qdii(instrument.name),
                lag_days=lag,
                first_nav_date=nav_days[0].isoformat() if nav_days else None,
                latest_nav_date=nav_days[-1].isoformat() if nav_days else None,
                nav_count=len(nav_days),
                effective_date=effective.isoformat() if effective else None,
            )
        )
    return SnapshotResponse(
        as_of=effective_as_of.isoformat(),
        trade_days=[d.isoformat() for d in shown_days],
        trade_day_count=len(union_days),
        truncated=truncated,
        funds=funds,
    )
