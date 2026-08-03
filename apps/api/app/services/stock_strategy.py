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
- 行业中性：以 universe 行业市值等权份额为基准，行业目标权重
  = universe 行业只数占比 × 投入仓位（近似流通市值中性，数据缺失时
  按只数口径），单行业权重不超过 max_industry_weight 硬上限；
- 行业内按复合分排名依次分配，单股权重 ≤ max_stock_weight（默认 5%），
  截断剩余顺延给同行业下一只；行业无可配股票或行业上限用满时，
  未用仓位转为现金（不跨行业倒灌，保持行业中性）；
- 月调仓：由回测层按月度节奏调用本模块，本模块保持无状态纯函数。

涨跌停与费用在回测层（stock_backtest）处理；本模块不感知成交价。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

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
MIN_KNOWN_INDUSTRY_RATIO = 0.5  # universe 已知行业占比下限（行业中性有效性的底线）


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
) -> PortfolioPlan:
    """由复合分排名构建目标组合（行业中性、单股/行业上限）。

    - scored：universe 内股票的复合分结果（compute_cross_section 输出）；
    - 行业目标份额 = universe 中行业只数占比（市值数据缺失时的等权近似），
      并受 max_industry_weight 硬上限约束（上限内按份额与上限取小）；
    - 行业内按复合分从高到低分配，单股 ≤ max_stock_weight，截断顺延；
    - 行业目标用不满（合格股票不足或全部触顶）的部分转为现金；
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
    # 行业份额：universe 只数占比（行业中性基准）
    industry_count: dict[str, int] = {}
    for code in universe_industries.values():
        industry_count[code] = industry_count.get(code, 0) + 1
    total_count = sum(industry_count.values())
    industry_quota = {
        industry: min(count / total_count, max_industry_weight)
        for industry, count in industry_count.items()
    }

    # 复合分排名（全局 top_n 入选）
    ranked = sorted(scored, key=lambda item: item.composite, reverse=True)[: max(top_n, 1)]

    by_industry: dict[str, list[factors.FactorResult]] = {}
    for item in ranked:
        industry = universe_industries.get(item.code, item.industry or "未知")
        by_industry.setdefault(industry, []).append(item)

    target: dict[str, float] = {}
    industry_weight: dict[str, float] = {}
    shortfall = 0.0
    for industry, quota in industry_quota.items():
        members = by_industry.get(industry, [])
        if not members:
            shortfall += quota  # 该行业无入选股票：配额留现金
            continue
        remaining = quota
        for item in members:
            if remaining <= 0:
                break
            weight = min(max_stock_weight, remaining)
            if weight <= 0:
                break
            target[item.code] = round(weight, 6)
            industry_weight[industry] = industry_weight.get(industry, 0.0) + weight
            remaining -= weight
        shortfall += max(remaining, 0.0)

    invested = sum(target.values())
    plan.target_weights = dict(sorted(target.items(), key=lambda kv: kv[1], reverse=True))
    plan.invested_weight = round(invested, 6)
    plan.industries = {key: round(value, 6) for key, value in sorted(industry_weight.items())}
    if shortfall > 1e-9:
        plan.warnings.append(
            f"行业配额/单股上限截断，{shortfall:.1%} 仓位留为现金（行业中性不跨行业倒灌）"
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
