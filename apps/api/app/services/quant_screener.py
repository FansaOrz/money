"""规则模型筛选器：候选池、横截面评分、市场状态过滤、目标权重与约束。

流程（全部为只读研究能力，不产生任何实盘下单行为）：
1. 候选池：默认当前持仓基金，也可显式指定 codes；
2. 市场分类：按基金名称关键词（见 quant_factors.classify_market），
   黄金/债券/货币/其他海外进入观察池，不参与股票基金横截面排名；
3. 因子：动量 0.5×R20+0.3×R60+0.2×R120、60 日风险调整动量、
   MA20/MA60 趋势（∈[-1,1]）、120 日最大回撤；
4. 横截面：动量/风险调整动量/回撤在全候选池取 z-score，按
   0.45/0.35/0.20/0.50 加权合成综合分，再在同一市场内取分位数；
5. 五档：前10% → +2、70%~90% → +1、30%~70% → 0、10%~30% → −1、
   后10% → −2；落 ±2 需趋势配合；
6. 市场状态：匹配指数 Risk-off 时正信号降一档（+2→+1，+1→0）；
7. 目标权重：全部样本满足的候选均参与五档分析并出现在结果中；仅综合分
   前 top_n 只进入目标组合分配权重（综合分归一，单基金 ≤25%、单一市场
   ≤50%），被截断的权重保留为现金，其余候选 target_weight 为 0、仅分析。

仅使用标准库 + SQLAlchemy；数据源为 FundNav / MarketIndex / IndexQuote。
"""

from __future__ import annotations

from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import FundNav, IndexQuote, Instrument, MarketIndex, Position
from app.schemas.quant import ScreenerItem, ScreenerRequest, ScreenerResponse
from app.services import quant_factors as factors
from app.services import quant_risk as risk
from app.services.quant import QuantError, _load_nav_series

# 读取净值/指数行情的最大条数（覆盖 120 日窗口 + 冗余）
NAV_LOAD_LIMIT = 400
# 参与五档排名的最低候选数：低于该值全部落中性档并提示样本不足
MIN_RANK_CANDIDATES = 10
# 单一市场内做本分位的最低有效样本数：低于该值时改用全权益池分位
# （全权益池同样不足时落中性档），避免小样本市场（极端为单成员市场
# 必然分位 0、恒落末档）产生误导性档位
MIN_MARKET_QUANTILE_SAMPLES = 5

MAX_FUND_WEIGHT = 0.25
MAX_MARKET_WEIGHT = 0.50

SCREENER_METHODOLOGY = (
    "规则模型 V1：动量 MOM=0.5×R20+0.3×R60+0.2×R120（窗口不足重新归一化）；"
    "60 日风险调整动量（日收益均值/标准差）；120 日最大回撤；MA20/MA60 趋势 ∈[-1,1]。"
    "综合分 = 0.45×z(MOM)+0.35×z(RAM60)+0.20×趋势+0.50×z(回撤)（横截面 z-score）。"
    "同类市场内按综合分分位数落五档：前10%→+2 值得研究加仓、70%~90%→+1 偏积极、"
    "30%~70%→0 中性持有、10%~30%→−1 偏谨慎、后10%→−2 值得研究减仓"
    "（±2 需趋势配合，否则回落 ±1）。市场 Risk-off 时正信号降一档。"
    "净值样本 ≥120 个交易日的候选全部参与五档分析；单一市场内有效样本 <5 时，"
    "该市场候选不做本市场分位，改用全权益池分位（全权益池同样 <5 时落中性档）；"
    "综合分前 top_n 只进入目标组合，目标权重按综合分归一，单基金 ≤25%、"
    "单一市场 ≤50%（多轮再分配直至约束满足），截断部分保留为现金；"
    "其余候选仅参与分析，目标权重为 0。"
    "黄金/债券/货币/其他海外为观察池，不参与股票基金横截面排名。"
    "幸存者偏差声明：候选池为当前持仓或当前指定基金，历史时点已清盘/调出池的"
    "基金不在样本内，历史分位与回测可能系统性偏好存活至今的基金。"
    "仅为研究信号，不构成投资建议，不产生任何自动交易。"
)


# ---------------------------------------------------------------------------
# 数据装载
# ---------------------------------------------------------------------------


def _load_candidates(
    db: Session, codes: list[str] | None
) -> tuple[list[Instrument], list[str]]:
    """装载候选基金：显式 codes 或全部持仓基金。返回 (基金列表, 警告)。"""
    warnings: list[str] = []
    if codes is not None:
        requested = list(dict.fromkeys(codes))  # 去重保持顺序
        rows = db.execute(
            select(Instrument).where(Instrument.code.in_(requested))
        ).scalars().all()
        by_code = {instrument.code: instrument for instrument in rows}
        missing = [code for code in requested if code not in by_code]
        if missing:
            warnings.append(f"以下基金代码未找到，已跳过：{', '.join(missing)}")
        instruments = [by_code[code] for code in requested if code in by_code]
        if not instruments:
            raise QuantError("指定的候选基金均未找到，请检查代码")
        return instruments, warnings

    rows = db.execute(
        select(Instrument)
        .join(Position, Position.instrument_id == Instrument.id)
        .group_by(Instrument.id)
        .order_by(Instrument.code)
    ).scalars().all()
    if not rows:
        raise QuantError("当前无持仓基金，请先导入持仓或显式指定候选 codes")
    return list(rows), warnings


def _load_benchmark_closes(
    db: Session, markets: set[str]
) -> tuple[dict[str, list[float]], set[str]]:
    """装载各市场基准指数的收盘价序列（升序）。

    返回 (市场 -> 收盘序列, 有行情的市场集合)；
    某市场配置了指数但库中无该指数时计入缺失集合（由调用方提示）。
    """
    codes: dict[str, str] = {}
    for market in sorted(markets):
        configured = factors.MARKET_BENCHMARKS.get(market)
        if configured:
            codes[market] = configured[0]
    if not codes:
        return {}, set()

    rows = db.execute(
        select(MarketIndex).where(MarketIndex.code.in_(list(codes.values())))
    ).scalars().all()
    index_by_code = {index.code: index for index in rows}

    series_by_market: dict[str, list[float]] = {}
    missing_markets: set[str] = set()
    for market, code in codes.items():
        index = index_by_code.get(code)
        if index is None:
            missing_markets.add(market)
            continue
        quote_rows = db.execute(
            select(IndexQuote.close)
            .where(IndexQuote.index_id == index.id)
            .order_by(IndexQuote.trade_date.desc())
            .limit(NAV_LOAD_LIMIT)
        ).all()
        # DESC LIMIT 保留最新行情，反转为升序供窗口指标使用
        values = [
            float(close)
            for (close,) in reversed(quote_rows)
            if close is not None and float(close) > 0
        ]
        if values:
            series_by_market[market] = values
    return series_by_market, missing_markets


# ---------------------------------------------------------------------------
# 因子计算与横截面评分
# ---------------------------------------------------------------------------


def _compute_factor_bundle(
    instrument: Instrument, series: list[tuple[date, float]]
) -> factors.FactorBundle:
    """计算单只基金的原始因子。"""
    values = [v for _, v in series]
    market = factors.classify_market(instrument.name)
    momentum, _ = factors.momentum_score(values)
    risk_adjusted = factors.risk_adjusted_momentum(values)
    trend, _ = factors.trend_strength(values)
    drawdown = factors.max_drawdown(values, window=factors.DRAWDOWN_WINDOW)
    configured = factors.MARKET_BENCHMARKS.get(market)
    return factors.FactorBundle(
        code=instrument.code,
        name=instrument.name,
        market=market,
        benchmark=configured[0] if configured else None,
        data_date=series[-1][0],
        sample_count=len(series),
        momentum=momentum,
        risk_adjusted=risk_adjusted,
        trend=trend,
        drawdown=drawdown,
    )


def _apply_cross_section(candidates: list[factors.FactorBundle]) -> None:
    """横截面 z-score、综合分与同市场分位数（就地更新 bundle）。

    单一市场内有效样本 < MIN_MARKET_QUANTILE_SAMPLES 时不做本市场分位
    （小样本分位无统计意义，单成员市场必然分位 0、恒落末档），改用
    全权益池分位；全权益池同样不足时保持 None（落中性档）并提示。
    """
    momentum_z = factors.zscores({b.code: b.momentum for b in candidates})
    risk_adj_z = factors.zscores({b.code: b.risk_adjusted for b in candidates})
    drawdown_z = factors.zscores({b.code: b.drawdown for b in candidates})
    for bundle in candidates:
        bundle.score = factors.composite_score(
            momentum_z.get(bundle.code),
            risk_adj_z.get(bundle.code),
            bundle.trend,
            drawdown_z.get(bundle.code),
        )
        if bundle.momentum is None:
            bundle.warnings.append("动量窗口样本不足，动量按横截面中性处理")
        if bundle.risk_adjusted is None:
            bundle.warnings.append("近60日日收益波动为 0，风险调整动量按横截面中性处理")

    # 同市场分位数（仅权益候选参与）
    markets = {b.market for b in candidates}
    pool_ranks: dict[str, float | None] | None = None  # 惰性计算全权益池分位
    for market in markets:
        group = [b for b in candidates if b.market == market]
        if len(group) >= MIN_MARKET_QUANTILE_SAMPLES:
            ranks = factors.quantile_ranks({b.code: b.score for b in group})
            for bundle in group:
                bundle.quantile = ranks.get(bundle.code)
            continue
        # 小样本市场：回退到全权益池分位或中性
        if len(candidates) >= MIN_MARKET_QUANTILE_SAMPLES:
            if pool_ranks is None:
                pool_ranks = factors.quantile_ranks({b.code: b.score for b in candidates})
            for bundle in group:
                bundle.quantile = pool_ranks.get(bundle.code)
                bundle.warnings.append(
                    f"{factors.market_label(market)}内有效样本仅 {len(group)} 只"
                    f"（<{MIN_MARKET_QUANTILE_SAMPLES}），改用全权益池分位（{len(candidates)} 只）落档"
                )
        else:
            for bundle in group:
                bundle.quantile = None
                bundle.warnings.append(
                    f"{factors.market_label(market)}内有效样本仅 {len(group)} 只"
                    f"且全权益池仅 {len(candidates)} 只（<{MIN_MARKET_QUANTILE_SAMPLES}），"
                    "分位样本不足，落中性档"
                )


def _apply_market_regimes(
    candidates: list[factors.FactorBundle],
    benchmark_closes: dict[str, list[float]],
    missing_benchmarks: set[str],
    global_warnings: list[str],
) -> None:
    """市场状态过滤：Risk-off 时正信号降一档；指数历史不足不调整并提示。"""
    regimes: dict[str, str] = {}
    reported_insufficient: set[str] = set()
    for market in {b.market for b in candidates}:
        if market in missing_benchmarks:
            global_warnings.append(
                f"{factors.market_label(market)}基准指数未配置行情，市场状态不调整"
            )
            regimes[market] = "insufficient"
            continue
        values = benchmark_closes.get(market)
        if not values:
            regimes[market] = "insufficient"
            global_warnings.append(
                f"{factors.market_label(market)}基准指数无行情数据，市场状态不调整"
            )
            continue
        regime, _ = factors.index_regime(values)
        regimes[market] = regime
        if regime == "insufficient" and market not in reported_insufficient:
            reported_insufficient.add(market)
            global_warnings.append(
                f"{factors.market_label(market)}基准指数历史不足 60 日（{len(values)} 条），"
                "市场状态不调整"
            )

    for bundle in candidates:
        regime = regimes.get(bundle.market, "insufficient")
        adjusted = factors.adjust_tier_for_regime(bundle.tier, regime)
        if adjusted != bundle.tier:
            bundle.reasons.append(
                f"{factors.market_label(bundle.market)}处于 Risk-off，正信号降一档"
            )
            bundle.regime_adjusted = True
            bundle.tier = adjusted


# ---------------------------------------------------------------------------
# 权重约束
# ---------------------------------------------------------------------------


def _apply_weight_constraints(
    candidates: list[factors.FactorBundle], top_n: int
) -> list[factors.FactorBundle]:
    """目标权重：综合分归一，单基金 ≤25%、单一市场 ≤50%、最多 top_n 只。

    多轮再分配（复用 quant_risk.apply_weight_caps 的瀑布实现）：每轮找出
    违例最严重的基金固定在其可达到的上限，释放的额度按当前权重比例分给
    其余未封顶基金，直至约束全部满足 —— 同分/集体触顶时也能正确收敛；
    无法再分配的截断部分保留为现金（合计 ≤1，不卖空）。
    返回按目标权重降序的入选列表。
    """
    ranked = sorted(candidates, key=lambda b: b.score, reverse=True)[:top_n]
    positives = [bundle.score for bundle in ranked if bundle.score > 0]
    offset = min(positives) if positives else 0.0
    raw = {bundle.code: max(bundle.score - offset, 0.0) for bundle in ranked}
    total_raw = sum(raw.values())
    if total_raw <= 0:
        raw = {bundle.code: 1.0 for bundle in ranked}
        total_raw = float(len(ranked))

    base = {code: value / total_raw for code, value in raw.items()}
    markets = {bundle.code: bundle.market for bundle in ranked}
    # 复用 V2 瀑布式封顶 helper：v1 的“家族”槽位即市场（市场合计 ≤50%），
    # v1 不做 QDII 合计约束（max_qdii=1.0 关闭该槽位）
    capped = risk.apply_weight_caps(
        base,
        families=markets,
        markets=markets,
        max_fund=MAX_FUND_WEIGHT,
        max_family=MAX_MARKET_WEIGHT,
        max_qdii=1.0,
    )
    for bundle in ranked:
        theoretical = base[bundle.code]
        final = capped.get(bundle.code, 0.0)
        bundle.target_weight = final
        if theoretical - final > 1e-9:
            bundle.weight_capped = True
            bundle.warnings.append(
                f"目标权重受约束截断：理论 {theoretical:.1%} → 实际 {final:.1%}，"
                "截断部分保留为现金"
            )

    selected_codes = {bundle.code for bundle in ranked}
    for bundle in candidates:
        if bundle.code not in selected_codes:
            bundle.target_weight = 0.0
    return ranked


# ---------------------------------------------------------------------------
# 理由组装
# ---------------------------------------------------------------------------


def _build_reasons(bundle: factors.FactorBundle) -> None:
    """在 bundle.reasons 前置因子的可解释说明。"""
    reasons: list[str] = []
    if bundle.momentum is not None:
        reasons.append(
            f"动量 MOM {bundle.momentum:+.2%}（0.5×R20+0.3×R60+0.2×R120）"
        )
    if bundle.risk_adjusted is not None:
        reasons.append(f"60 日风险调整动量 {bundle.risk_adjusted:+.3f}（日收益均值/标准差）")
    if bundle.trend is not None:
        trend_text = "多头排列" if bundle.trend > 0 else ("空头排列" if bundle.trend < 0 else "多空交织")
        reasons.append(f"MA20/MA60 趋势 {bundle.trend:+.2f}（{trend_text}）")
    if bundle.drawdown is not None:
        reasons.append(f"近 120 日最大回撤 {bundle.drawdown:.1%}")
    if bundle.quantile is not None:
        reasons.append(
            f"{factors.market_label(bundle.market)}内综合分分位数 {bundle.quantile:.0%}，"
            f"落档 {factors.tier_label(bundle.tier)}"
        )
    bundle.reasons = reasons + bundle.reasons


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------


def run_screener(db: Session, req: ScreenerRequest) -> ScreenerResponse:
    """规则模型筛选入口：候选池 → 因子 → 横截面 → 五档 → 市场过滤 → 权重约束。"""
    global_warnings: list[str] = []
    instruments, load_warnings = _load_candidates(db, req.codes)
    global_warnings.extend(load_warnings)

    candidates: list[factors.FactorBundle] = []
    observe_pool: list[factors.FactorBundle] = []
    excluded_count = 0
    sample_counts: dict[str, int] = {}

    for instrument in instruments:
        series = _load_nav_series(db, instrument.id, limit=NAV_LOAD_LIMIT)
        sample_counts[instrument.code] = len(series)
        if len(series) < req.min_samples:
            excluded_count += 1
            continue
        bundle = _compute_factor_bundle(instrument, series)
        if factors.is_equity_market(bundle.market):
            candidates.append(bundle)
        else:
            observe_pool.append(bundle)

    insufficient_ranking = 0 < len(candidates) < MIN_RANK_CANDIDATES
    if insufficient_ranking:
        global_warnings.append(
            f"候选池仅 {len(candidates)} 只（少于 {MIN_RANK_CANDIDATES} 只），"
            "样本不足，全部落中性档，不区分五档"
        )

    if candidates:
        _apply_cross_section(candidates)
        for bundle in candidates:
            bundle.tier = factors.tier_from_quantile(bundle.quantile, bundle.trend)
        benchmark_markets = {b.market for b in candidates}
        benchmark_closes, missing_benchmarks = _load_benchmark_closes(db, benchmark_markets)
        _apply_market_regimes(
            candidates, benchmark_closes, missing_benchmarks, global_warnings
        )
        if insufficient_ranking:
            # 候选池过小：分位数无统计意义，强制中性并隐藏分位数
            for bundle in candidates:
                bundle.tier = 0
                bundle.quantile = None
        for bundle in candidates:
            _build_reasons(bundle)
        selected = _apply_weight_constraints(candidates, req.top_n)
    else:
        selected = []
        if observe_pool:
            global_warnings.append("候选均为观察池资产（黄金/债券/货币/其他海外），不参与五档排名")

    as_of = (
        max(b.data_date for b in candidates + observe_pool).isoformat()
        if (candidates or observe_pool)
        else None
    )

    # 全部完成分析的候选都返回：目标组合标的按目标权重降序在前，
    # 仅分析标的按综合分降序在后（target_weight 为 0）
    ranked_selected = sorted(selected, key=lambda item: item.target_weight, reverse=True)
    selected_codes = {b.code for b in ranked_selected}
    analysis_only = sorted(
        (b for b in candidates if b.code not in selected_codes),
        key=lambda item: item.score,
        reverse=True,
    )
    if analysis_only:
        global_warnings.append(
            f"按 top_n={req.top_n} 进入目标组合 {len(ranked_selected)} 只，"
            f"其余 {len(analysis_only)} 只候选仅参与分析、目标权重为 0"
        )
    ordered = ranked_selected + analysis_only
    items = [
        ScreenerItem(
            code=b.code,
            name=b.name,
            market=b.market,  # type: ignore[arg-type]
            benchmark=b.benchmark,
            score=round(b.score, 6),
            quantile=round(b.quantile, 6) if b.quantile is not None else None,
            tier=b.tier,
            label=b.label,
            target_weight=b.target_weight,
            in_target=b.code in selected_codes,
            reasons=b.reasons,
            factors=b.factors_dict(),
            data_date=b.data_date.isoformat(),
            warnings=b.warnings,
        )
        for b in ordered
    ]

    return ScreenerResponse(
        as_of=as_of,
        methodology=SCREENER_METHODOLOGY,
        candidate_count=len(candidates),
        excluded_count=excluded_count,
        observe_count=len(observe_pool),
        selected_count=len(items),
        allocation_count=len(ranked_selected),
        items=items,
        warnings=global_warnings,
    )
