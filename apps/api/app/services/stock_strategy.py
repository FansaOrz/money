"""A股多因子选股策略（纯函数）：动态 universe 过滤与组合构建。

universe 过滤（每个打分日 T 重新计算，即动态 universe）：
1. ST/退市风险：股票名称含 "ST"（含 *ST）剔除；
2. 停牌：T 日无 bar 或 T 日 bar 标记 suspended 剔除；
3. 次新股：上市未满 120 个交易日（以行情序列中 ≤T 的 bar 数近似，
   list_date 存在时以其为起点）剔除；
4. 流动性：近 20 个交易日日均成交额 < min_avg_amount（默认 5000 万元）剔除；
5. 样本：非停牌正收盘价历史 < 260 个交易日（12-1 动量所需）剔除。

组合构建：
- 入选：复合分排名前 top_n（默认 30）；
- 行业中性：按信号日自由流通市值形成行业基准，市值覆盖低于 95% 时拒绝
  构建，禁止回退只数口径；单行业权重不超过 max_industry_weight 硬上限；
- 目标函数同时考虑复合分、预期波动、换手和流动性成本；约束单股、行业、
  市值、Beta、流动性、ADV 参与率和最少持仓数；
- 持仓保留区、入选/剔除双阈值和最小交易权重抑制边界抖动；
  基准行业配额未用满时，在入选行业间按剩余容量再分配，但始终遵守
  单股和单行业硬上限；只有全部容量不足时才持有现金；
- 月调仓：由回测层按月度节奏调用本模块，本模块保持无状态纯函数。

涨跌停、成交容量、动态滑点、费用和公司行为在回测/前向执行层处理；
本模块仅生成目标权重。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import date

import numpy as np

from app.services import stock_factors as factors
from app.services.stock_repository import (
    NamePeriod,
    StockBar,
    StockInfo,
    is_st_name,
    st_status_as_of,
)

# ---------------------------------------------------------------------------
# 常量（策略默认参数）
# ---------------------------------------------------------------------------

MIN_LIST_AGE_DAYS = 120  # 上市未满 120 个交易日剔除
LIQUIDITY_WINDOW = 20  # 流动性窗口（交易日）
DEFAULT_MIN_AVG_AMOUNT = 5e7  # 日均成交额下限：5000 万元
DEFAULT_TOP_N = 30  # 入选股票数上限
DEFAULT_MAX_STOCK_WEIGHT = 0.05  # 单股权重上限 5%
DEFAULT_MAX_INDUSTRY_WEIGHT = 0.20  # 单行业权重上限 20%
MIN_HISTORY_BARS = factors.MIN_HISTORY_DAYS
MIN_KNOWN_INDUSTRY_RATIO = 0.95  # 正式行业中性所需的信号日分类覆盖
CONSTRAINT_NUMERIC_TOLERANCE = 1e-5  # 优化器/浮点计算的约束验收容差
MAX_INDUSTRY_ACTIVE_WEIGHT = 0.03  # 相对基准的单行业主动偏离上限


# ---------------------------------------------------------------------------
# 过滤结果
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class UniverseFilter:
    """一只股票在打分日 T 的 universe 判定结果。"""

    code: str
    passed: bool
    reasons: tuple[str, ...] = ()  # 未通过原因（可解释）


@dataclass
class PortfolioPlan:
    """一期调仓的组合计划：目标权重与可解释说明。"""

    as_of: date
    target_weights: dict[str, float] = field(default_factory=dict)
    invested_weight: float = 0.0  # 股票仓位合计（其余为现金）
    industries: dict[str, float] = field(default_factory=dict)  # 行业权重分布
    filters: list[UniverseFilter] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    industry_known_ratio: float = 1.0  # universe 已知行业占比（1.0 = 全部已知）
    diagnostics: dict[str, object] = field(default_factory=dict)


class IndustryCoverageError(ValueError):
    """行业覆盖不足：行业中性失效，路由层/回测层转换为显式拒绝。"""


# ---------------------------------------------------------------------------
# universe 过滤（纯函数）
# ---------------------------------------------------------------------------


def _tradable_bars_upto(bars: list[StockBar], as_of: date) -> list[StockBar]:
    """≤T 的非停牌正收盘 bar（升序）。"""
    return sorted(
        (
            bar
            for bar in bars
            if bar.trade_date <= as_of and not bar.suspended and bar.close > 0
        ),
        key=lambda bar: bar.trade_date,
    )


def filter_universe_stock(
    info: StockInfo,
    bars: list[StockBar],
    as_of: date,
    min_avg_amount: float = DEFAULT_MIN_AVG_AMOUNT,
    min_list_age_days: int = MIN_LIST_AGE_DAYS,
    min_history_bars: int = MIN_HISTORY_BARS,
    name_periods: list[NamePeriod] | None = None,
    research_bars: list[StockBar] | None = None,
) -> UniverseFilter:
    """单只股票的 universe 判定：ST / 停牌 / 次新 / 流动性 / 样本。

    bars 为执行口径（raw）历史：停牌/成交额流动性判定使用；
    research_bars（qfq 研究口径）存在时，上市天数/历史样本深度改用它
    （与因子打分同源）；ST 判定按 as_of 当日名称（名称历史优先，
    无历史覆盖时回退当前名称）。本函数自行按 ≤T 截断。
    """
    reasons: list[str] = []

    if st_status_as_of(info.name, name_periods, as_of):
        reasons.append("ST/退市风险标识")

    history = _tradable_bars_upto(bars, as_of)
    if not history:
        reasons.append("无可用历史行情")
        return UniverseFilter(code=info.code, passed=False, reasons=tuple(reasons))

    # 停牌：T 日恰好有 bar 且被标记停牌，或 T 日根本无 bar（数据缺失按停牌处理）
    bar_by_date = {bar.trade_date: bar for bar in bars if bar.trade_date <= as_of}
    latest_bar = bar_by_date.get(max(bar_by_date)) if bar_by_date else None
    if latest_bar is None or latest_bar.trade_date < as_of or latest_bar.suspended:
        reasons.append("T 日停牌或无行情")

    # 上市天数/历史样本：研究口径（与因子打分同源）优先
    depth_bars = (
        _tradable_bars_upto(research_bars, as_of)
        if research_bars is not None
        else history
    )
    if info.list_date is not None:
        listed_bars = [bar for bar in depth_bars if bar.trade_date >= info.list_date]
        age = len(listed_bars)
    else:
        age = len(depth_bars)
    if age < min_list_age_days:
        reasons.append(f"上市未满 {min_list_age_days} 个交易日（当前 {age}）")

    if len(depth_bars) < min_history_bars:
        reasons.append(
            f"历史样本不足 {min_history_bars} 个交易日（当前 {len(depth_bars)}）"
        )

    window = history[-LIQUIDITY_WINDOW:]
    amounts = [bar.amount for bar in window if bar.amount is not None]
    if not amounts:
        reasons.append("无成交额数据，流动性无法确认")
    else:
        avg_amount = sum(amounts) / len(amounts)
        if avg_amount < min_avg_amount:
            reasons.append(
                f"近{LIQUIDITY_WINDOW}日日均成交额 {avg_amount / 1e8:.2f} 亿 < "
                f"{min_avg_amount / 1e8:.2f} 亿"
            )

    return UniverseFilter(code=info.code, passed=not reasons, reasons=tuple(reasons))


def build_universe(
    infos: list[StockInfo],
    bars_by_code: dict[str, list[StockBar]],
    as_of: date,
    min_avg_amount: float = DEFAULT_MIN_AVG_AMOUNT,
    name_histories: dict[str, list[NamePeriod]] | None = None,
    research_bars_by_code: dict[str, list[StockBar]] | None = None,
) -> tuple[list[StockInfo], list[UniverseFilter]]:
    """全市场 universe 过滤，返回 (通过名单, 全部判定明细)。

    bars_by_code 为执行口径（raw）；research_bars_by_code（qfq）存在时
    样本深度判定与因子打分同源；name_histories 存在时按 as_of 当日
    名称判定历史 ST。
    """
    passed: list[StockInfo] = []
    filters: list[UniverseFilter] = []
    for info in infos:
        result = filter_universe_stock(
            info,
            bars_by_code.get(info.code, []),
            as_of,
            min_avg_amount,
            name_periods=(name_histories or {}).get(info.code),
            research_bars=(
                research_bars_by_code.get(info.code)
                if research_bars_by_code is not None
                else None
            ),
        )
        filters.append(result)
        if result.passed:
            passed.append(info)
    return passed, filters


# ---------------------------------------------------------------------------
# 组合构建：行业中性 + 单股/行业上限
# ---------------------------------------------------------------------------


def industry_known_ratio(infos: list[StockInfo]) -> float:
    """universe 中行业已知（非「未知」）的只数占比；空 universe 记 1.0。"""
    if not infos:
        return 1.0
    known = sum(1 for info in infos if (info.industry or "未知") != "未知")
    return known / len(infos)


def check_industry_coverage(
    infos: list[StockInfo],
    min_ratio: float = MIN_KNOWN_INDUSTRY_RATIO,
) -> float:
    """行业覆盖门槛：已知行业占比 < min_ratio 时抛 IndustryCoverageError。

    行业中性（行业内 winsorize+z-score、行业份额配额）在多数股票行业
    未知时退化为单组横截面，正式回测结果无解释力，应显式拒绝而非
    静默降级。返回已知占比（供 warning 展示）。
    """
    ratio = industry_known_ratio(infos)
    if ratio < min_ratio:
        raise IndustryCoverageError(
            f"行业数据覆盖不足：universe 已知行业占比 {ratio:.0%} < "
            f"{min_ratio:.0%}，行业中性失效，拒绝正式回测/组合构建；"
            f"请先同步行业分类数据（申万/中信）"
        )
    return ratio


def build_portfolio(
    scored: list[factors.FactorResult],
    universe: list[StockInfo],
    as_of: date,
    top_n: int = DEFAULT_TOP_N,
    max_stock_weight: float = DEFAULT_MAX_STOCK_WEIGHT,
    max_industry_weight: float = DEFAULT_MAX_INDUSTRY_WEIGHT,
    enforce_industry_coverage: bool = True,
    current_weights: dict[str, float] | None = None,
    retention_buffer: int = 5,
    minimum_trade_weight: float = 0.002,
    portfolio_value: float = 1_000_000.0,
    max_adv_participation: float = 0.10,
    minimum_holdings: int = 20,
    max_annual_volatility: float = 0.25,
    max_tracking_error: float = 0.15,
    use_convex_optimizer: bool = False,
) -> PortfolioPlan:
    """由复合分排名构建目标组合（行业中性、单股/行业上限）。

    - scored：universe 内股票的复合分结果（compute_cross_section 输出）；
    - 行业目标份额按信号日自由流通市值（缺失时用总市值）计算，覆盖
      低于 95% 时拒绝组合构建，不以只数口径制造伪中性；
    - 行业内按复合分从高到低分配，单股 ≤ max_stock_weight，截断顺延；
    - 行业基准配额用不满时，在入选行业的剩余风险容量内再分配；
      全部入选标的容量仍不足时，剩余部分转为现金；
    - 行业覆盖：已知行业占比低于 MIN_KNOWN_INDUSTRY_RATIO 时抛
      IndustryCoverageError（enforce_industry_coverage=False 仅记 warning，
      供研究性因子查询降级使用）。
    """
    plan = PortfolioPlan(as_of=as_of)
    if not scored or not universe:
        plan.warnings.append("universe 为空或无有效打分结果，本期持有现金")
        return plan

    known_ratio = industry_known_ratio(universe)
    plan.industry_known_ratio = round(known_ratio, 6)
    if enforce_industry_coverage:
        check_industry_coverage(universe)
    elif known_ratio < MIN_KNOWN_INDUSTRY_RATIO:
        plan.warnings.append(
            f"行业数据覆盖不足（已知行业占比 {known_ratio:.0%} < "
            f"{MIN_KNOWN_INDUSTRY_RATIO:.0%}），行业中性退化为单组横截面，"
            f"结果仅供研究参考"
        )
    elif known_ratio < 1.0 - 1e-9:
        plan.warnings.append(
            f"{1.0 - known_ratio:.0%} 的 universe 股票行业未知，"
            f"按「未知」单组参与行业中性（数据降级）"
        )

    universe_industries = {info.code: (info.industry or "未知") for info in universe}
    market_cap_by_code = {
        item.code: item.float_market_cap or item.market_cap
        for item in scored
        if (item.float_market_cap or item.market_cap) is not None
        and (item.float_market_cap or item.market_cap) > 0
    }
    cap_coverage = sum(
        code in market_cap_by_code for code in universe_industries
    ) / len(universe_industries)
    if cap_coverage < 0.95:
        raise IndustryCoverageError(
            f"信号日自由流通/总市值覆盖仅 {cap_coverage:.0%} < 95%，"
            "拒绝按股票只数近似行业基准"
        )
    industry_cap: dict[str, float] = {}
    for code, industry in universe_industries.items():
        market_cap = market_cap_by_code.get(code)
        if market_cap is not None:
            industry_cap[industry] = industry_cap.get(industry, 0.0) + market_cap
    total_cap = sum(industry_cap.values())
    industry_quota = {
        industry: min(value / total_cap, max_industry_weight)
        for industry, value in industry_cap.items()
    }

    # 复合分排名（先执行因子数据覆盖门禁，再取全局 top_n）
    eligible = [item for item in scored if item.eligible]
    current_weights = current_weights or {}

    def optimization_score(item: factors.FactorResult) -> float:
        daily_volatility = max(-float(item.raw.get("volatility_60") or 0.0), 0.0)
        turnover_penalty = 0.0 if current_weights.get(item.code, 0.0) > 0 else 0.01
        illiquidity_penalty = (
            0.0
            if item.average_daily_amount is not None and item.average_daily_amount > 0
            else 0.05
        )
        return (
            item.composite
            - 2.0 * daily_volatility
            - turnover_penalty
            - illiquidity_penalty
        )

    all_ranked = sorted(eligible, key=optimization_score, reverse=True)
    rank_by_code = {item.code: index + 1 for index, item in enumerate(all_ranked)}
    # 若只取全局 Top N，可能完全遗漏基准权重大于主动偏离上限的行业，
    # 随后的正式优化器又只允许在已选标的内调权，必然得到 infeasible。
    # 先为重要行业保留该行业最高分股票，再按全局优化分补足 Top N。
    selected_codes: set[str] = set()
    if use_convex_optimizer:
        actual_industry_weights = {
            industry: value / total_cap
            for industry, value in industry_cap.items()
            if total_cap > 0
        }
        material_industries = [
            industry
            for industry, _weight in sorted(
                actual_industry_weights.items(),
                key=lambda item: item[1],
                reverse=True,
            )
            if actual_industry_weights[industry]
            > MAX_INDUSTRY_ACTIVE_WEIGHT + CONSTRAINT_NUMERIC_TOLERANCE
        ]
        for industry in material_industries:
            required_weight = max(
                actual_industry_weights[industry] - MAX_INDUSTRY_ACTIVE_WEIGHT,
                0.0,
            )
            required_seats = max(
                int(np.ceil(required_weight / max(max_stock_weight, 1e-12))),
                1,
            )
            candidates = [
                item
                for item in all_ranked
                if universe_industries.get(item.code, item.industry or "未知")
                == industry
            ]
            for candidate in candidates[:required_seats]:
                if len(selected_codes) >= max(top_n, 1):
                    break
                selected_codes.add(candidate.code)
    for item in all_ranked:
        if len(selected_codes) >= max(top_n, 1):
            break
        selected_codes.add(item.code)
    if current_weights:
        selected_codes.update(
            code
            for code, weight in current_weights.items()
            if weight > 0
            and rank_by_code.get(code, len(all_ranked) + 1)
            <= max(top_n, 1) + max(retention_buffer, 0)
        )
    ranked = [item for item in all_ranked if item.code in selected_codes][
        : max(top_n + max(retention_buffer, 0), 1)
    ]
    excluded_for_data = len(scored) - len(eligible)
    if excluded_for_data:
        plan.warnings.append(f"{excluded_for_data} 只股票因因子数据覆盖不足不参与组合")

    by_industry: dict[str, list[factors.FactorResult]] = {}
    for item in ranked:
        industry = universe_industries.get(item.code, item.industry or "未知")
        by_industry.setdefault(industry, []).append(item)

    # 容量上限需要覆盖全部合格候选，而不只是初始 Top N。后续风格中性化
    # 可能要把同业的高分标的替换为稍低分、但更接近指数暴露的标的。
    stock_weight_limit = {
        item.code: min(
            max_stock_weight,
            (
                item.average_daily_amount * max_adv_participation / portfolio_value
                if item.average_daily_amount is not None and portfolio_value > 0
                else max_stock_weight
            ),
        )
        for item in all_ranked
    }

    target: dict[str, float] = {}
    industry_weight: dict[str, float] = {}
    for industry, quota in industry_quota.items():
        members = by_industry.get(industry, [])
        if not members:
            continue
        remaining = quota
        active = list(members)
        while remaining > 1e-12 and active:
            equal_addition = remaining / len(active)
            next_active: list[factors.FactorResult] = []
            allocated = 0.0
            for item in active:
                room = stock_weight_limit[item.code] - target.get(item.code, 0.0)
                addition = min(equal_addition, max(room, 0.0))
                if addition > 0:
                    target[item.code] = target.get(item.code, 0.0) + addition
                    allocated += addition
                if room - addition > 1e-12:
                    next_active.append(item)
            if allocated <= 1e-12:
                break
            remaining -= allocated
            active = next_active
        industry_weight[industry] = quota - max(remaining, 0.0)

    # 基准行业配额常因全局 top_n 未覆盖所有行业而留下大量现金。将剩余
    # 资金在“已入选且仍有容量”的行业/股票间回补，同时保持两级硬上限。
    remaining_cash = max(1.0 - sum(target.values()), 0.0)
    for _ in range(max(len(ranked), 1) + 1):
        if remaining_cash <= 1e-10:
            break
        capacity_by_industry: dict[str, float] = {}
        members_by_industry: dict[str, list[factors.FactorResult]] = {}
        for item in ranked:
            industry = universe_industries.get(item.code, item.industry or "未知")
            stock_room = max(
                stock_weight_limit[item.code] - target.get(item.code, 0.0), 0.0
            )
            industry_room = max(
                max_industry_weight - industry_weight.get(industry, 0.0), 0.0
            )
            if stock_room <= 1e-12 or industry_room <= 1e-12:
                continue
            members_by_industry.setdefault(industry, []).append(item)
        for industry, members in members_by_industry.items():
            stock_capacity = sum(
                max(stock_weight_limit[item.code] - target.get(item.code, 0.0), 0.0)
                for item in members
            )
            capacity_by_industry[industry] = min(
                stock_capacity,
                max(max_industry_weight - industry_weight.get(industry, 0.0), 0.0),
            )
        total_capacity = sum(capacity_by_industry.values())
        if total_capacity <= 1e-12:
            break
        to_allocate = min(remaining_cash, total_capacity)
        allocated = 0.0
        for industry, capacity in capacity_by_industry.items():
            industry_add = to_allocate * capacity / total_capacity
            members = members_by_industry[industry]
            rooms = {
                item.code: max(
                    stock_weight_limit[item.code] - target.get(item.code, 0.0), 0.0
                )
                for item in members
            }
            room_total = sum(rooms.values())
            for item in members:
                addition = industry_add * rooms[item.code] / room_total
                target[item.code] = target.get(item.code, 0.0) + addition
                allocated += addition
            industry_weight[industry] = (
                industry_weight.get(industry, 0.0) + industry_add
            )
        remaining_cash = max(remaining_cash - allocated, 0.0)

    target = {
        code: round(weight, 6) for code, weight in target.items() if weight > 1e-9
    }
    rounded_total = sum(target.values())
    if rounded_total > 1.0 and target:
        code = max(target, key=target.get)
        target[code] = round(target[code] - (rounded_total - 1.0), 6)

    # 风格暴露诊断：基准按自由流通市值，组合按目标权重。
    benchmark_weights = (
        {
            code: cap / sum(market_cap_by_code.values())
            for code, cap in market_cap_by_code.items()
        }
        if market_cap_by_code and sum(market_cap_by_code.values()) > 0
        else {}
    )
    scored_by_code = {item.code: item for item in scored}
    exposure_fields = {
        "size": "size_exposure",
        "beta": "beta_exposure",
        "liquidity": "liquidity_exposure",
    }
    normalized_exposures: dict[str, dict[str, float]] = {}
    for label, field_name in exposure_fields.items():
        values = {
            item.code: float(value)
            for item in scored
            if (value := getattr(item, field_name)) is not None
        }
        if len(values) < 2:
            normalized_exposures[label] = values
            continue
        mean = sum(values.values()) / len(values)
        variance = sum((value - mean) ** 2 for value in values.values()) / len(values)
        std = variance**0.5
        normalized_exposures[label] = {
            code: (value - mean) / std if std > 0 else 0.0
            for code, value in values.items()
        }

    style_tolerances = {"size": 0.20, "beta": 0.15, "liquidity": 0.20}
    benchmark_style_targets: dict[str, float] = {}
    for label, exposures in normalized_exposures.items():
        available = {
            code: weight
            for code, weight in benchmark_weights.items()
            if code in exposures
        }
        total = sum(available.values())
        if total > 0:
            benchmark_style_targets[label] = (
                sum(weight * exposures[code] for code, weight in available.items())
                / total
            )

    def style_deviations(weights: dict[str, float]) -> dict[str, float]:
        invested = sum(weights.values())
        if invested <= 0:
            return {}
        return {
            label: (
                sum(
                    weight * exposures[code]
                    for code, weight in weights.items()
                    if code in exposures
                )
                / invested
                - benchmark
            )
            for label, benchmark in benchmark_style_targets.items()
            if (exposures := normalized_exposures[label])
            and all(code in exposures for code in weights)
        }

    def style_violation_score(deviations: dict[str, float]) -> float:
        return sum(
            max(abs(value) - style_tolerances[label], 0.0) ** 2
            for label, value in deviations.items()
        )

    # 初始 Top N 往往偏向中小盘或某种流动性风格，仅在已入选股票之间搬
    # 权重无法接近市值加权指数。用同一行业候选做确定性的局部替换：
    # 保持持仓数、行业权重和单股权重不变，只在能严格降低超限暴露时换股。
    # 候选池保留足够深度，同时仍按优化分排序，避免中性化吞噬全部 alpha。
    candidate_pool = all_ranked[: max(top_n * 8, 200)]
    scored_lookup = {item.code: item for item in all_ranked}
    for _round in range(max(len(target) * 4, 1)):
        current_deviations = style_deviations(target)
        current_violation = style_violation_score(current_deviations)
        if current_violation <= 1e-12:
            break
        best_swap: tuple[str, str, float, float] | None = None
        for source, source_weight in sorted(target.items()):
            source_industry = universe_industries.get(source, "未知")
            for destination_item in candidate_pool:
                destination = destination_item.code
                if destination in target:
                    continue
                if universe_industries.get(destination, "未知") != source_industry:
                    continue
                if stock_weight_limit.get(destination, 0.0) + 1e-12 < source_weight:
                    continue
                if any(
                    source not in exposures or destination not in exposures
                    for exposures in normalized_exposures.values()
                ):
                    continue
                candidate = dict(target)
                candidate.pop(source)
                candidate[destination] = source_weight
                candidate_violation = style_violation_score(style_deviations(candidate))
                improvement = current_violation - candidate_violation
                if improvement <= 1e-12:
                    continue
                factor_loss = max(
                    optimization_score(scored_lookup[source])
                    - optimization_score(destination_item),
                    0.0,
                )
                choice = (source, destination, improvement, factor_loss)
                if best_swap is None or (
                    improvement > best_swap[2] + 1e-12
                    or (
                        abs(improvement - best_swap[2]) <= 1e-12
                        and factor_loss < best_swap[3]
                    )
                ):
                    best_swap = choice
        if best_swap is None:
            break
        source, destination, _improvement, _factor_loss = best_swap
        target[destination] = target.pop(source)

    # 在同一行业内部搬移权重，实际约束市值/Beta/流动性相对基准偏离；
    # 不改变行业权重，并继续遵守单股上限。
    for _round in range(100):
        moved_in_round = False
        for label in exposure_fields:
            exposures = normalized_exposures[label]
            benchmark_available = {
                code: weight
                for code, weight in benchmark_weights.items()
                if code in exposures
            }
            benchmark_total = sum(benchmark_available.values())
            invested_now = sum(target.values())
            if benchmark_total <= 0 or invested_now <= 0:
                continue
            benchmark_value = (
                sum(
                    weight * exposures[code]
                    for code, weight in benchmark_available.items()
                )
                / benchmark_total
            )
            portfolio_value = (
                sum(
                    weight * exposures[code]
                    for code, weight in target.items()
                    if code in exposures
                )
                / invested_now
            )
            tolerance = style_tolerances[label]
            deviation = portfolio_value - benchmark_value
            if abs(deviation) <= tolerance:
                continue
            best_pair: tuple[str, str, float] | None = None
            for source, source_weight in target.items():
                if source_weight <= 1e-9 or source not in exposures:
                    continue
                for destination in target:
                    if destination not in exposures:
                        continue
                    if universe_industries.get(source) != universe_industries.get(
                        destination
                    ):
                        continue
                    room = stock_weight_limit[destination] - target[destination]
                    if room <= 1e-9:
                        continue
                    improvement = (
                        exposures[source] - exposures[destination]
                        if deviation > 0
                        else exposures[destination] - exposures[source]
                    )
                    if improvement <= 0:
                        continue
                    capacity = min(source_weight, room)
                    score = improvement * capacity
                    if best_pair is None or score > best_pair[2]:
                        best_pair = (source, destination, score)
            if best_pair is None:
                continue
            source, destination, _score = best_pair
            exposure_gap = abs(exposures[source] - exposures[destination])
            required = (
                max(abs(deviation) - tolerance, 0.0) * invested_now / exposure_gap
            )
            moved = min(
                required,
                max(target[source] - minimum_trade_weight, 0.0),
                stock_weight_limit[destination] - target[destination],
            )
            target[source] -= moved
            target[destination] += moved
            moved_in_round = moved_in_round or moved > 1e-12
            if target[source] <= 1e-10:
                target.pop(source, None)
        if not moved_in_round:
            break

    required_holdings = min(
        max(top_n, 1), max(minimum_holdings, 1), max(len(eligible), 1)
    )
    if target and len(target) < required_holdings:
        plan.warnings.append(
            f"优化后仅 {len(target)} 只可满足全部风险/容量约束，低于最少"
            f"持仓 {required_holdings} 只，本期拒绝形成集中组合并持有现金"
        )
        target = {}
        industry_weight = {}

    benchmark_exposures: dict[str, float | None] = {}
    portfolio_exposures: dict[str, float | None] = {}
    deviations: dict[str, float | None] = {}
    for label, field_name in exposure_fields.items():
        exposures = normalized_exposures[label]
        benchmark_values = [
            (benchmark_weights.get(code, 0.0), value)
            for code, value in exposures.items()
        ]
        benchmark_total = sum(weight for weight, _value in benchmark_values)
        benchmark_value = (
            sum(weight * value for weight, value in benchmark_values) / benchmark_total
            if benchmark_total > 0
            else None
        )
        portfolio_values = [
            (weight, exposures[code])
            for code, weight in target.items()
            if code in exposures
        ]
        portfolio_total = sum(weight for weight, _value in portfolio_values)
        portfolio_value = (
            sum(weight * value for weight, value in portfolio_values) / portfolio_total
            if portfolio_total > 0
            else None
        )
        benchmark_exposures[label] = benchmark_value
        portfolio_exposures[label] = portfolio_value
        deviations[label] = (
            portfolio_value - benchmark_value
            if portfolio_value is not None and benchmark_value is not None
            else None
        )
    exposure_violations = {
        label: deviation
        for label, deviation in deviations.items()
        if deviation is not None
        and abs(deviation) > style_tolerances[label] + CONSTRAINT_NUMERIC_TOLERANCE
    }
    if target and exposure_violations:
        message = "、".join(
            f"{label}={deviation:+.6f}"
            for label, deviation in exposure_violations.items()
        )
        if use_convex_optimizer:
            plan.warnings.append(
                "启发式初始组合尚未满足市值/Beta/流动性硬约束，"
                f"交由正式凸优化器继续求解：{message}"
            )
        else:
            plan.warnings.append(
                "市值/Beta/流动性硬约束无法同时满足，本期拒绝形成伪中性组合：" + message
            )
            target = {}
            industry_weight = {}

    daily_risk_by_code = {
        item.code: -float(item.raw["volatility_60"])
        for item in scored
        if item.raw.get("volatility_60") is not None
        and -float(item.raw["volatility_60"]) > 0
    }
    fallback_risk = (
        sorted(daily_risk_by_code.values())[len(daily_risk_by_code) // 2]
        if daily_risk_by_code
        else 0.02
    )

    risk_codes = sorted(set(target) | set(benchmark_weights))
    risk_lookup = {item.code: item for item in scored}
    industries_for_risk = sorted(
        {risk_lookup[code].industry for code in risk_codes if code in risk_lookup}
    )
    market_variance = max(fallback_risk**2 * 0.35, 1e-8)
    industry_variance = max(fallback_risk**2 * 0.15, 1e-8)
    covariance = np.zeros((len(risk_codes), len(risk_codes)), dtype=float)
    for left_index, left in enumerate(risk_codes):
        left_item = risk_lookup.get(left)
        left_beta = (
            float(left_item.beta_exposure)
            if left_item is not None and left_item.beta_exposure is not None
            else 1.0
        )
        left_industry = left_item.industry if left_item is not None else "未知"
        for right_index, right in enumerate(risk_codes):
            right_item = risk_lookup.get(right)
            right_beta = (
                float(right_item.beta_exposure)
                if right_item is not None and right_item.beta_exposure is not None
                else 1.0
            )
            right_industry = right_item.industry if right_item is not None else "未知"
            covariance[left_index, right_index] = (
                left_beta * right_beta * market_variance
                + (
                    industry_variance
                    if left_industry == right_industry and left_industry != "未知"
                    else 0.0
                )
            )
        total_variance = daily_risk_by_code.get(left, fallback_risk) ** 2
        covariance[left_index, left_index] += max(
            total_variance - covariance[left_index, left_index],
            total_variance * 0.20,
        )
    if len(risk_codes):
        eigenvalues, eigenvectors = np.linalg.eigh((covariance + covariance.T) / 2.0)
        covariance = (
            eigenvectors @ np.diag(np.maximum(eigenvalues, 1e-12)) @ eigenvectors.T
        )

    optimizer_report: dict[str, object] | None = None
    if target and risk_codes and use_convex_optimizer:
        from app.services.convex_portfolio_optimizer import (
            estimate_trade_cost_rates,
            optimize_portfolio,
        )

        alpha_values = [
            0.002 * risk_lookup[code].composite
            if code in risk_lookup and code in target
            else 0.0
            for code in risk_codes
        ]
        alpha_errors = [
            0.001
            + 0.003
            * (
                1.0
                - min(
                    max(risk_lookup[code].data_coverage, 0.0),
                    1.0,
                )
            )
            if code in risk_lookup
            else 0.01
            for code in risk_codes
        ]
        effective_portfolio_value = float(portfolio_value or 1_000_000.0)
        average_amounts = [
            (
                risk_lookup[code].average_daily_amount
                if code in risk_lookup
                and risk_lookup[code].average_daily_amount is not None
                else effective_portfolio_value
            )
            for code in risk_codes
        ]
        expected_trade_values = [
            abs(target.get(code, 0.0) - (current_weights or {}).get(code, 0.0))
            * effective_portfolio_value
            for code in risk_codes
        ]
        cost_rates = estimate_trade_cost_rates(
            [1.0] * len(risk_codes),
            average_amounts,
            expected_trade_values,
        )
        industry_matrix = np.asarray(
            [
                [
                    1.0
                    if code in risk_lookup and risk_lookup[code].industry == industry
                    else 0.0
                    for industry in industries_for_risk
                ]
                for code in risk_codes
            ],
            dtype=float,
        )
        benchmark_industry = [
            sum(
                benchmark_weights.get(code, 0.0)
                for code in risk_codes
                if code in risk_lookup and risk_lookup[code].industry == industry
            )
            for industry in industries_for_risk
        ]
        style_matrix = np.asarray(
            [
                [
                    normalized_exposures[label].get(code, 0.0)
                    for label in ("size", "beta", "liquidity")
                ]
                for code in risk_codes
            ],
            dtype=float,
        )
        benchmark_style = [
            sum(
                benchmark_weights.get(code, 0.0)
                * normalized_exposures[label].get(code, 0.0)
                for code in risk_codes
            )
            for label in ("size", "beta", "liquidity")
        ]
        optimizer_report = optimize_portfolio(
            codes=risk_codes,
            alpha=alpha_values,
            alpha_standard_errors=alpha_errors,
            covariance=covariance,
            current_weights=[
                (current_weights or {}).get(code, 0.0) for code in risk_codes
            ],
            benchmark_weights=[benchmark_weights.get(code, 0.0) for code in risk_codes],
            linear_cost_rates=cost_rates["linear_cost_rates"],
            impact_cost_rates=cost_rates["impact_cost_rates"],
            industry_exposures=(
                industry_matrix if len(eligible) >= minimum_holdings else None
            ),
            benchmark_industry_exposures=(
                benchmark_industry if len(eligible) >= minimum_holdings else None
            ),
            style_exposures=(
                style_matrix if len(eligible) >= minimum_holdings else None
            ),
            benchmark_style_exposures=(
                benchmark_style if len(eligible) >= minimum_holdings else None
            ),
            adv_weight_limits=[
                (
                    max(
                        stock_weight_limit.get(code, max_stock_weight),
                        (current_weights or {}).get(code, 0.0),
                    )
                    if code in target or (current_weights or {}).get(code, 0.0) > 0
                    else 0.0
                )
                for code in risk_codes
            ],
            asset_weight_limits=[
                (
                    stock_weight_limit.get(code, max_stock_weight)
                    if code in target
                    else 0.0
                )
                for code in risk_codes
            ],
            max_stock_weight=max_stock_weight,
            max_industry_active_weight=MAX_INDUSTRY_ACTIVE_WEIGHT,
            max_style_active_exposure=[0.20, 0.15, 0.20],
            max_tracking_error=max_tracking_error,
            max_annual_volatility=max_annual_volatility,
            max_turnover=2.0,
            minimum_cash=max(1.0 - sum(target.values()), 0.0),
            maximum_cash=max(1.0 - sum(target.values()), 0.0),
        )
        if optimizer_report["passed"] is True:
            original_target_codes = set(target)
            target = {
                code: min(float(weight), max_stock_weight)
                for code, weight in dict(optimizer_report["weights"]).items()
                if code in original_target_codes and float(weight) >= 1e-7
            }
            industry_weight = {}
            for code, weight in target.items():
                industry = risk_lookup[code].industry if code in risk_lookup else "未知"
                industry_weight[industry] = industry_weight.get(industry, 0.0) + weight
        else:
            plan.warnings.append(
                "正式凸优化不可行，本期持有现金；诊断="
                + str(optimizer_report.get("infeasibility"))
            )
            target = {}
            industry_weight = {}

    # 凸优化会重写目标权重，必须以最终权重重新计算风格暴露；否则诊断仍展示
    # 启发式初始组合，既可能误报通过，也可能误报超限。
    if target and optimizer_report is not None:
        portfolio_exposures = {}
        deviations = {}
        for label, exposures in normalized_exposures.items():
            portfolio_values = [
                (weight, exposures[code])
                for code, weight in target.items()
                if code in exposures
            ]
            portfolio_total = sum(weight for weight, _value in portfolio_values)
            portfolio_value = (
                sum(weight * value for weight, value in portfolio_values)
                / portfolio_total
                if portfolio_total > 0
                else None
            )
            portfolio_exposures[label] = portfolio_value
            benchmark_value = benchmark_exposures.get(label)
            deviations[label] = (
                portfolio_value - benchmark_value
                if portfolio_value is not None and benchmark_value is not None
                else None
            )
        final_exposure_violations = {
            label: deviation
            for label, deviation in deviations.items()
            if deviation is not None
            and abs(deviation) > style_tolerances[label] + CONSTRAINT_NUMERIC_TOLERANCE
        }
        if final_exposure_violations:
            plan.warnings.append(
                "正式凸优化结果仍违反市值/Beta/流动性硬约束，本期持有现金："
                + "、".join(
                    f"{label}={deviation:+.6f}"
                    for label, deviation in final_exposure_violations.items()
                )
            )
            target = {}
            industry_weight = {}

    def annual_volatility(weights: dict[str, float]) -> float:
        if not risk_codes:
            return 0.0
        vector = np.asarray(
            [weights.get(code, 0.0) for code in risk_codes], dtype=float
        )
        return float(np.sqrt(max(vector @ covariance @ vector, 0.0) * 252.0))

    expected_annual_vol = annual_volatility(target)
    if (
        target
        and max_annual_volatility > 0
        and expected_annual_vol > max_annual_volatility
    ):
        scale = max_annual_volatility / expected_annual_vol
        target = {code: weight * scale for code, weight in target.items()}
        industry_weight = {
            industry: weight * scale for industry, weight in industry_weight.items()
        }
        expected_annual_vol = annual_volatility(target)
        plan.warnings.append(
            f"预期年化波动超过 {max_annual_volatility:.1%}，"
            f"股票仓位按 {scale:.1%} 缩放，其余持有现金"
        )
    active_codes = set(target) | set(benchmark_weights)
    expected_tracking_error = annual_volatility(
        {
            code: target.get(code, 0.0) - benchmark_weights.get(code, 0.0)
            for code in active_codes
        }
    )
    if (
        target
        and len(eligible) >= minimum_holdings
        and max_tracking_error > 0
        and expected_tracking_error > max_tracking_error
    ):
        plan.warnings.append(
            f"预期跟踪误差 {expected_tracking_error:.1%} 超过硬上限"
            f" {max_tracking_error:.1%}，本期持有现金"
        )
        target = {}
        industry_weight = {}
    invested = sum(target.values())
    plan.target_weights = dict(
        sorted(target.items(), key=lambda kv: kv[1], reverse=True)
    )
    plan.invested_weight = round(invested, 6)
    plan.industries = {
        key: round(value, 6) for key, value in sorted(industry_weight.items())
    }
    effective_n = (
        1.0 / sum(weight * weight for weight in target.values()) if target else 0.0
    )
    estimated_turnover = 0.5 * sum(
        abs(target.get(code, 0.0) - (current_weights or {}).get(code, 0.0))
        for code in set(target) | set(current_weights or {})
    )
    expected_score = sum(
        weight * scored_by_code[code].composite
        for code, weight in target.items()
        if code in scored_by_code
    )
    lowvol_risk = [
        daily_risk_by_code[item.code]
        for item in scored
        if item.code in target and item.code in daily_risk_by_code
    ]
    expected_daily_vol = (
        sum(
            target[item.code] * daily_risk_by_code[item.code]
            for item in scored
            if item.code in target and item.code in daily_risk_by_code
        )
        if lowvol_risk
        else None
    )
    from app.services.portfolio_risk_controls import (
        capacity_curve,
        stress_test,
        tail_risk,
    )

    target_codes = list(target)
    stress_report = stress_test(
        codes=target_codes,
        weights=[target[code] for code in target_codes],
        position_values=[
            target[code] * float(portfolio_value or 1_000_000.0)
            for code in target_codes
        ],
        adv_amounts=[
            (
                risk_lookup[code].average_daily_amount
                if code in risk_lookup
                and risk_lookup[code].average_daily_amount is not None
                else float(portfolio_value or 1_000_000.0)
            )
            for code in target_codes
        ],
        industry_by_code={
            code: (risk_lookup[code].industry if code in risk_lookup else "未知")
            for code in target_codes
        },
    )
    simulated_returns: list[float] = []
    if target_codes and risk_codes:
        target_vector = np.asarray([target.get(code, 0.0) for code in risk_codes])
        simulated = np.random.default_rng(20260805).multivariate_normal(
            np.zeros(len(risk_codes)),
            covariance,
            size=2000,
        )
        simulated_returns = (simulated @ target_vector).tolist()
    tail_report = tail_risk(simulated_returns)
    capacity_report = capacity_curve(
        codes=target_codes,
        target_weights=[target[code] for code in target_codes],
        adv_amounts=[
            (
                risk_lookup[code].average_daily_amount
                if code in risk_lookup
                and risk_lookup[code].average_daily_amount is not None
                else float(portfolio_value or 1_000_000.0)
            )
            for code in target_codes
        ],
        capital_levels=[
            float(portfolio_value or 1_000_000.0) * multiplier
            for multiplier in (0.5, 1.0, 2.0, 5.0, 10.0)
        ],
        gross_expected_return=max(expected_score * 0.01, 0.0),
    )
    plan.diagnostics = {
        "objective": "factor_score-risk-turnover-cost",
        "expected_factor_score": round(expected_score, 8),
        "estimated_turnover": round(estimated_turnover, 6),
        "effective_holdings": round(effective_n, 4),
        "herfindahl": round(sum(weight * weight for weight in target.values()), 8),
        "expected_daily_volatility": expected_daily_vol,
        "expected_annual_volatility": expected_annual_vol,
        "expected_tracking_error": expected_tracking_error,
        "risk_model": "MARKET_INDUSTRY_SPECIFIC_PSD_V1",
        "risk_model_assets": risk_codes,
        "risk_model_industries": industries_for_risk,
        "risk_covariance_shape": list(covariance.shape),
        "risk_covariance_sha256": hashlib.sha256(
            np.ascontiguousarray(covariance).tobytes()
        ).hexdigest(),
        "risk_covariance_diagonal_range": (
            [
                float(np.diag(covariance).min()),
                float(np.diag(covariance).max()),
            ]
            if covariance.size
            else None
        ),
        "convex_optimizer": optimizer_report,
        "max_annual_volatility": max_annual_volatility,
        "max_tracking_error": max_tracking_error,
        "stress_test": stress_report,
        "stress_loss": stress_report.get("worst_portfolio_pnl_rate"),
        "tail_risk": tail_report,
        "capacity_curve": capacity_report,
        "benchmark_exposures": benchmark_exposures,
        "portfolio_exposures": portfolio_exposures,
        "exposure_deviations": deviations,
        "float_market_cap_coverage": round(cap_coverage, 6),
        "retention_buffer": retention_buffer,
        "minimum_trade_weight": minimum_trade_weight,
        "max_adv_participation": max_adv_participation,
        "capacity_constrained_count": sum(
            stock_weight_limit.get(code, 0.0) < max_stock_weight - 1e-12
            for code in target
        ),
    }
    for label, deviation in deviations.items():
        tolerance = 0.20 if label != "beta" else 0.15
        if (
            deviation is not None
            and abs(deviation) > tolerance + CONSTRAINT_NUMERIC_TOLERANCE
        ):
            plan.warnings.append(
                f"{label} 相对基准暴露偏离 {deviation:+.3f} 超过"
                f" {tolerance:.2f}，受入选数量/行业/单股约束未能完全中性"
            )
    final_shortfall = max(1.0 - invested, 0.0)
    if final_shortfall > CONSTRAINT_NUMERIC_TOLERANCE:
        plan.warnings.append(
            f"单股/单行业风险容量不足，{final_shortfall:.1%} 仓位留为现金"
        )
    return plan


# ---------------------------------------------------------------------------
# 月调仓日历
# ---------------------------------------------------------------------------


def month_ends(days: list[date]) -> list[date]:
    """交易日历中每月最后一个交易日（升序去重）。"""
    result: list[date] = []
    for day in sorted(days):
        if result and result[-1].year == day.year and result[-1].month == day.month:
            result[-1] = day
        else:
            result.append(day)
    return result


def is_st_suspended_on(bar: StockBar | None) -> bool:
    """成交日行情缺失或停牌 → 不可成交。"""
    return bar is None or bar.suspended


__all__ = [
    "DEFAULT_MAX_INDUSTRY_WEIGHT",
    "DEFAULT_MAX_STOCK_WEIGHT",
    "DEFAULT_MIN_AVG_AMOUNT",
    "DEFAULT_TOP_N",
    "MIN_KNOWN_INDUSTRY_RATIO",
    "MIN_LIST_AGE_DAYS",
    "IndustryCoverageError",
    "PortfolioPlan",
    "UniverseFilter",
    "build_portfolio",
    "build_universe",
    "check_industry_coverage",
    "filter_universe_stock",
    "industry_known_ratio",
    "is_st_name",
    "month_ends",
]
