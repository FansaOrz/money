"""A股多因子研究路由：/api/stocks/research/* 因子 / 信号 / 回测 / universe。

全部为只读研究能力，不产生任何实盘下单行为。
数据经 app.services.stock_repository 动态装载（显式注入 > 注册工厂 >
约定模块探测 > 内置 SQL 适配器）；仓储完全不可用时返回 400。
"""

from datetime import date
from statistics import fmean

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.schemas.stocks import (
    StockBacktestRequest,
    StockBacktestResult,
    StockBacktestSummary,
    StockCurvePoint,
    StockFactorRow,
    StockFactorsRequest,
    StockFactorsResponse,
    StockRebalanceDetail,
    StockSignalItem,
    StockSignalsRequest,
    StockSignalsResponse,
    StockTradeRecord,
    StockValidationStats,
)
from app.services import quant_stats as stats
from app.services import stock_backtest as backtest_service
from app.services import stock_factors as factors_service
from app.services import stock_strategy as strategy_service
from app.services.stock_repository import StockRepository, load_repository

router = APIRouter(prefix="/stocks/research", tags=["stocks-research"])

FACTOR_METHODOLOGY = (
    "A股多因子横截面：行业内 1%/99% winsorize 后 z-score（行业中性化），"
    "五族复合 quality30%（ROE/毛利率/经营现金流利润比/负债率取负）、"
    "value25%（EP/BP/估值历史分位）、12-1 动量20%、趋势15%（MA20/MA60）、"
    "低波动10%（60 日日收益波动取负）；缺失族按可用权重归一化。"
    "基本面按 available_at ≤ 打分日做 PIT 过滤，行情仅用打分日及之前数据，"
    "无未来数据。universe 动态过滤：ST/停牌/上市未满 120 交易日/"
    "近20日日均成交额不足/历史样本不足。仅为研究输出，不构成投资建议。"
)
BACKTEST_METHODOLOGY = (
    "A股多因子月调仓回测：每月最后一个交易日（T）收盘后按复合分构建"
    "行业中性目标组合（单股 ≤5%、单行业 ≤20%），T+1 交易日按开盘价成交"
    "（缺失回退收盘）；涨跌停（默认 ±9.8%）与停牌不可成交，订单顺延至"
    "下一交易日重试；费用含双边佣金（万 2.5，最低 5 元）、卖出印花税"
    "（0.05%）与双边滑点（0.1%）；停牌日前收盘盯市。基准为 universe 等权"
    "买入持有（B0），指定指数且数据可用时为指数买入持有。walk-forward 口径："
    "每期持仓仅由当期信号日及之前的 PIT 数据决定，持有期为样本外；"
    "validation 统计各期复合分 vs 下一期前瞻收益的 Rank IC 与五档单调性。"
    "仅为研究回测，不构成投资建议，不产生任何自动交易。"
)


# ---------------------------------------------------------------------------
# 依赖：动态装载仓储
# ---------------------------------------------------------------------------


def get_stock_repository(db: Session = Depends(get_db)) -> StockRepository:
    """按优先级装载股票数据仓储；不可用时 400。"""
    repository = load_repository(db)
    if repository is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "股票数据仓储不可用：stock data 模块尚未落地。可通过 "
                "register_repository_factory / app.services.stock_repository."
                "get_repository 接入，或先同步 stock_master / 日线数据湖。"
            ),
        )
    return repository


def _parse_day(value: str, field_name: str) -> date:
    try:
        return date.fromisoformat(value[:10])
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"{field_name} 日期格式错误：{value}，应为 YYYY-MM-DD",
        ) from exc


# ---------------------------------------------------------------------------
# 共用编排：universe 过滤 + 横截面打分
# ---------------------------------------------------------------------------


def _score_universe(
    repository: StockRepository,
    req: StockFactorsRequest,
) -> tuple[date, list, list, list[str], list]:
    """装载数据 → universe 过滤 → 行业内复合分。

    返回 (打分日, universe 股票, FactorResult 列表, warnings, 被剔除判定)。
    研究因子使用研究口径（qfq 优先，经 MarketPanel 双口径装载）；
    universe 过滤的停牌/流动性使用执行口径（raw）。
    """
    as_of = _parse_day(req.as_of, "as_of")
    warnings: list[str] = []

    infos = repository.list_stocks(req.candidate_codes)
    if not infos:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="股票清单为空：数据仓储尚无股票元信息，或 candidate_codes 无匹配",
        )
    codes = [info.code for info in infos]
    panel = backtest_service.build_panel(repository, codes, None, as_of)
    bars_by_code = panel.bars_by_code
    research_by_code = panel.research_bars_by_code or None

    fundamentals_by_code = backtest_service.load_fundamentals_by_code(repository, codes)

    if req.apply_universe_filter:
        universe, filters = strategy_service.build_universe(
            infos,
            bars_by_code,
            as_of,
            req.min_avg_amount,
            name_histories=panel.name_histories,
            research_bars_by_code=research_by_code,
        )
    else:
        universe, filters = infos, []
    excluded = [item for item in filters if not item.passed]

    contexts = [
        factors_service.build_context(
            info,
            panel.research_series(info.code),
            fundamentals_by_code.get(info.code, []),
            as_of,
        )
        for info in universe
    ]
    contexts = [
        ctx
        for ctx in contexts
        if factors_service.history_depth(ctx) >= factors_service.MIN_HISTORY_DAYS
    ]
    dropped = len(universe) - len(contexts)
    if dropped:
        warnings.append(f"{dropped} 只股票因历史样本不足未参与打分")

    weights = None
    if req.weights is not None:
        weights = {
            "quality": req.weights.quality,
            "value": req.weights.value,
            "momentum": req.weights.momentum,
            "trend": req.weights.trend,
            "lowvol": req.weights.lowvol,
        }
    scored = factors_service.compute_cross_section(contexts, as_of, weights=weights)
    return as_of, universe, scored, warnings, excluded


# ---------------------------------------------------------------------------
# POST /factors
# ---------------------------------------------------------------------------


@router.post("/factors", response_model=StockFactorsResponse)
def compute_factors(
    payload: StockFactorsRequest,
    repository: StockRepository = Depends(get_stock_repository),
) -> StockFactorsResponse:
    """A股多因子横截面：行业内 winsorize+z-score 五族复合分。

    打分日 as_of 仅使用该日及之前的行情与 available_at ≤ as_of 的 PIT
    财务快照（无未来数据）；universe 动态过滤 ST/停牌/次新/低流动性。
    响应按复合分降序，行数受控截断。
    """
    as_of, universe, scored, warnings, excluded = _score_universe(repository, payload)

    ranked = sorted(scored, key=lambda item: item.composite, reverse=True)
    truncated = len(ranked) > factors_service.MAX_FACTOR_ROWS
    rows = [
        StockFactorRow(
            code=item.code,
            name=item.name,
            industry=item.industry,
            raw=item.raw,
            zscores=item.zscores,
            quality=item.quality,
            value=item.value,
            momentum=item.momentum,
            trend=item.trend,
            lowvol=item.lowvol,
            composite=round(item.composite, 6),
            rank=index + 1,
            data_warnings=item.data_warnings,
        )
        for index, item in enumerate(ranked[: factors_service.MAX_FACTOR_ROWS])
    ]

    exclusion_reasons: dict[str, int] = {}
    for item in excluded:
        reason = item.reasons[0] if item.reasons else "未知原因"
        exclusion_reasons[reason] = exclusion_reasons.get(reason, 0) + 1

    return StockFactorsResponse(
        as_of=as_of.isoformat(),
        universe_count=len(universe),
        excluded_count=len(excluded),
        exclusion_reasons=exclusion_reasons,
        industry_count=len({item.industry for item in ranked}),
        factor_weights=(
            {
                "quality": payload.weights.quality,
                "value": payload.weights.value,
                "momentum": payload.weights.momentum,
                "trend": payload.weights.trend,
                "lowvol": payload.weights.lowvol,
            }
            if payload.weights is not None
            else dict(factors_service.DEFAULT_FAMILY_WEIGHTS)
        ),
        rows=rows,
        truncated=truncated,
        methodology=FACTOR_METHODOLOGY,
        warnings=warnings,
    )


# ---------------------------------------------------------------------------
# POST /signals
# ---------------------------------------------------------------------------


@router.post("/signals", response_model=StockSignalsResponse)
def current_signals(
    payload: StockSignalsRequest,
    repository: StockRepository = Depends(get_stock_repository),
) -> StockSignalsResponse:
    """当期目标信号：因子横截面 + 行业中性目标组合（月调仓口径）。

    与回测共用同一套打分/过滤/约束逻辑；trade_date 为打分日之后
    第一个交易日（T+1），仓储无交易日历时为 None。仅为研究信号，
    不构成投资建议。
    """
    as_of, universe, scored, warnings, _excluded = _score_universe(repository, payload)
    try:
        plan = strategy_service.build_portfolio(
            scored,
            universe,
            as_of,
            top_n=payload.top_n,
            max_stock_weight=payload.max_stock_weight,
            max_industry_weight=payload.max_industry_weight,
        )
    except strategy_service.IndustryCoverageError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc
    warnings.extend(plan.warnings)

    rank_by_code = {
        item.code: index + 1
        for index, item in enumerate(
            sorted(scored, key=lambda entry: entry.composite, reverse=True)
        )
    }
    score_by_code = {item.code: item for item in scored}
    selected: list[StockSignalItem] = []
    for code, weight in plan.target_weights.items():
        item = score_by_code[code]
        reasons = [f"复合分 {item.composite:.3f}，横截面第 {rank_by_code[code]} 名"]
        strongest = max(
            (
                (family, getattr(item, family))
                for family in ("quality", "value", "momentum", "trend", "lowvol")
                if getattr(item, family) is not None
            ),
            key=lambda pair: pair[1],
            default=None,
        )
        if strongest is not None:
            reasons.append(f"最强因子族：{strongest[0]}（z={strongest[1]:.2f}）")
        reasons.append(f"行业 {item.industry}，行业中性目标权重 {weight:.2%}")
        selected.append(
            StockSignalItem(
                code=code,
                name=item.name,
                industry=item.industry,
                composite=round(item.composite, 6),
                rank=rank_by_code[code],
                weight=weight,
                quality=item.quality,
                value=item.value,
                momentum=item.momentum,
                trend=item.trend,
                lowvol=item.lowvol,
                reasons=reasons,
            )
        )

    trade_date = repository.trade_calendar(as_of, None).next_trade_day(as_of)
    return StockSignalsResponse(
        as_of=as_of.isoformat(),
        trade_date=trade_date.isoformat() if trade_date else None,
        universe_count=len(universe),
        selected=selected,
        invested_weight=plan.invested_weight,
        industry_weights=plan.industries,
        methodology=FACTOR_METHODOLOGY,
        warnings=warnings,
    )


# ---------------------------------------------------------------------------
# POST /backtest
# ---------------------------------------------------------------------------


def _annual_vol(returns: list[float]) -> float | None:
    if len(returns) < 2:
        return None
    mean = fmean(returns)
    variance = sum((r - mean) ** 2 for r in returns) / (len(returns) - 1)
    return variance**0.5 * (252**0.5)


def _summarize(values: list[float]) -> StockBacktestSummary:
    """由净值序列（起点任意值）汇总指标。"""
    if len(values) < 2 or values[0] <= 0:
        return StockBacktestSummary()
    returns = [
        values[i] / values[i - 1] - 1.0
        for i in range(1, len(values))
        if values[i - 1] > 0
    ]
    total = values[-1] / values[0] - 1.0
    max_dd = stats.max_drawdown(values)
    return StockBacktestSummary(
        total_return=total,
        annual_return=stats.annualized_return(total, len(values) - 1),
        annual_volatility=_annual_vol(returns),
        max_drawdown=max_dd,
        sharpe=stats.sharpe_ratio(returns),
        win_rate=(sum(1 for r in returns if r > 0) / len(returns)) if returns else None,
        cvar95=stats.cvar95(returns),
        calmar=stats.calmar_ratio(total, len(values) - 1, max_dd),
    )


@router.post("/backtest", response_model=StockBacktestResult)
def run_backtest(
    payload: StockBacktestRequest,
    repository: StockRepository = Depends(get_stock_repository),
) -> StockBacktestResult:
    """月调仓多因子回测：T 日信号 T+1 成交，涨跌停/停牌顺延，含费用。

    walk-forward 口径：每期持仓仅由当期信号日及之前的 PIT 数据决定；
    输出策略/基准汇总、validation 统计（Rank IC、五档单调性）、
    净值曲线（抽样）、调仓明细与成交记录（规模受控）。
    """
    config = backtest_service.BacktestConfig(
        start=_parse_day(payload.start_date, "start_date"),
        end=_parse_day(payload.end_date, "end_date"),
        initial_capital=payload.initial_capital,
        top_n=payload.top_n,
        max_stock_weight=payload.max_stock_weight,
        max_industry_weight=payload.max_industry_weight,
        min_avg_amount=payload.min_avg_amount,
        price_limit=payload.price_limit,
        candidate_codes=tuple(payload.candidate_codes) if payload.candidate_codes else None,
        benchmark_index=payload.benchmark_index,
        cost=backtest_service.CostModel(
            commission_rate=payload.cost.commission_rate,
            min_commission=payload.cost.min_commission,
            stamp_tax_rate=payload.cost.stamp_tax_rate,
            slippage_rate=payload.cost.slippage_rate,
        ),
    )
    try:
        outcome = backtest_service.run_backtest(config=config, repository=repository)
    except strategy_service.IndustryCoverageError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc
    except backtest_service.BacktestError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc

    strategy_summary = _summarize(outcome.equity)
    # 基准净值起点 1.0；换算为金额口径与策略对齐总收益
    benchmark_values = [v * outcome.equity[0] for v in outcome.benchmark]
    benchmark_summary = _summarize(benchmark_values)
    benchmark_returns = [
        outcome.benchmark[i] / outcome.benchmark[i - 1] - 1.0
        for i in range(1, len(outcome.benchmark))
        if outcome.benchmark[i - 1] > 0
    ]
    excess = (
        strategy_summary.total_return - benchmark_summary.total_return
        if strategy_summary.total_return is not None
        and benchmark_summary.total_return is not None
        else None
    )
    validation = backtest_service.validation_stats(
        outcome.scores_by_date, outcome.forward_returns
    )

    rebalances: list[StockRebalanceDetail] = []
    trades: list[StockTradeRecord] = []
    for detail in outcome.rebalances:
        fills = [
            StockTradeRecord(
                signal_date=fill.signal_date.isoformat(),
                fill_date=fill.fill_date.isoformat(),
                code=fill.code,
                action=fill.action,  # type: ignore[arg-type]
                price=round(fill.price, 4),
                shares=round(fill.shares, 2),
                amount=round(fill.amount, 2),
                fee=round(fill.fee, 2),
                reason=fill.reason,
            )
            for fill in detail.fills
        ]
        rebalances.append(
            StockRebalanceDetail(
                signal_date=detail.signal_date.isoformat(),
                target=detail.target,
                cash_weight=detail.cash_weight,
                turnover=round(detail.turnover, 6),
                blocked_codes=detail.blocked_codes,
                fills=fills,
                warnings=detail.warnings,
            )
        )
        trades.extend(fills)
    trades = trades[: backtest_service.MAX_TRADE_RECORDS]

    curve = [
        StockCurvePoint(date=day, equity=equity, benchmark=benchmark)
        for day, equity, benchmark in backtest_service.sample_curve(
            outcome.calendar, outcome.equity, outcome.benchmark
        )
    ]

    return StockBacktestResult(
        params={
            "top_n": payload.top_n,
            "max_stock_weight": payload.max_stock_weight,
            "max_industry_weight": payload.max_industry_weight,
            "min_avg_amount": payload.min_avg_amount,
            "price_limit": payload.price_limit,
            "benchmark_index": payload.benchmark_index,
            "candidate_codes": (
                list(payload.candidate_codes) if payload.candidate_codes else None
            ),
            "cost": payload.cost.model_dump(),
        },
        start_date=outcome.calendar[0].isoformat(),
        end_date=outcome.calendar[-1].isoformat(),
        initial_capital=payload.initial_capital,
        final_value=outcome.final_value,
        strategy=strategy_summary,
        benchmark=benchmark_summary,
        benchmark_kind=outcome.benchmark_kind,
        excess_return=excess,
        information_ratio=stats.information_ratio(
            outcome.daily_returns, benchmark_returns
        ),
        total_fees=outcome.total_fees,
        avg_turnover=round(outcome.avg_turnover, 6),
        rebalance_count=len(outcome.rebalances),
        validation=StockValidationStats(**validation),
        curve=curve,
        rebalances=rebalances,
        trades=trades,
        methodology=BACKTEST_METHODOLOGY,
        warnings=outcome.warnings,
    )


# ---------------------------------------------------------------------------
# GET /universe（辅助：查看某日动态 universe 判定）
# ---------------------------------------------------------------------------


@router.get("/universe")
def universe_snapshot(
    as_of: str = Query(description="判定日 YYYY-MM-DD"),
    min_avg_amount: float = Query(default=5e7, gt=0),
    repository: StockRepository = Depends(get_stock_repository),
) -> dict:
    """动态 universe 判定快照：每只股票通过/剔除原因（可解释）。"""
    day = _parse_day(as_of, "as_of")
    infos = repository.list_stocks(None)
    if not infos:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="股票清单为空"
        )
    codes = [info.code for info in infos]
    bars_by_code: dict[str, list] = {}
    for bar in repository.daily_bars(codes, None, day):
        bars_by_code.setdefault(bar.code, []).append(bar)
    name_fn = getattr(repository, "name_histories", None)
    histories: dict = {}
    if callable(name_fn):
        try:
            histories = dict(name_fn(codes))
        except Exception:  # noqa: BLE001 - 可选扩展失败降级（ST 按当前名称）
            histories = {}
    _passed, filters = strategy_service.build_universe(
        infos, bars_by_code, day, min_avg_amount, name_histories=histories
    )
    return {
        "as_of": day.isoformat(),
        "total": len(filters),
        "passed": sum(1 for f in filters if f.passed),
        "items": [
            {"code": f.code, "passed": f.passed, "reasons": list(f.reasons)}
            for f in filters
        ],
    }
