"""A股多因子引擎（纯函数）：行业内 winsorize + z-score 中性化与五族复合分。

因子族与权重（合计 100%）：
- quality  30%：ROE、销售毛利率、经营现金流/净利润（ocf_to_profit）、
  资产负债率（取负值，负债越低得分越高）四项等权；
- value    25%：EP（盈利收益率）、BP（账面市值比）、估值历史分位
  （EP+BP 平均值在其自身历史序列中的分位数，PIT 对齐）三项等权；
- momentum 20%：12-1 动量，T-21 收盘 / T-252 收盘 - 1（跳过最近 21 个交易日）；
- trend    15%：现价与 MA20/MA60 多空顺序，∈ [-1, 1]；
- lowvol   10%：近 60 日日收益波动率的相反数（波动越低得分越高）。

横截面处理：每个打分日、每个行业内部，对每个原始因子先做
1%/99% winsorize（样本 <8 时跳过，避免小样本误伤），再做 z-score；
行业内样本 <2 或标准差为 0 时该行业该因子 z 一律记 0（无区分度）。
缺失值保持缺失，复合时按可用子因子权重重新归一化。

无未来数据保证：价格因子只使用打分日 T 及之前的收盘价（动量跳过
最近 21 日且窗口固定在 T 截止的序列尾部）；基本面因子只使用
available_at ≤ T 的最新一条 PIT 快照（最新财报的估值分位历史窗口
也按同一 T 过滤）。

因子族权重通过 compute_cross_section(weights=...) 参数化（walk-forward
网格搜索可注入），缺省为模块常量 30/25/20/15/10，不修改全局状态。

仅使用标准库，不访问数据库，便于单测。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date
from statistics import fmean

from app.services.stock_repository import Fundamentals, StockBar, StockInfo

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

TRADING_DAYS_PER_YEAR = 252

# 因子族权重（缺省值；调用方可经 weights 参数覆盖，不改全局状态）
WEIGHT_QUALITY = 0.30
WEIGHT_VALUE = 0.25
WEIGHT_MOMENTUM = 0.20
WEIGHT_TREND = 0.15
WEIGHT_LOWVOL = 0.10

DEFAULT_FAMILY_WEIGHTS: dict[str, float] = {
    "quality": WEIGHT_QUALITY,
    "value": WEIGHT_VALUE,
    "momentum": WEIGHT_MOMENTUM,
    "trend": WEIGHT_TREND,
    "lowvol": WEIGHT_LOWVOL,
}

# 子因子方向：1 为越大越好，-1 为越小越好
QUALITY_DIRECTIONS: dict[str, int] = {
    "roe": 1,
    "roa": 1,
    "gross_margin": 1,
    "net_margin": 1,
    "ocf_to_profit": 1,
    "accruals": -1,
    "earnings_stability": 1,
    "margin_change": 1,
    "debt_ratio": -1,
    "financial_roe": 1,
    "financial_roa": 1,
    "financial_earnings_stability": 1,
}
VALUE_DIRECTIONS: dict[str, int] = {
    "ep": 1,
    "bp": 1,
    "sales_yield": 1,
    "dividend_yield": 1,
    "fcf_yield": 1,
    "loss_profitability": 1,
    "valuation_percentile": 1,
}

MOMENTUM_WINDOW = 252  # 12 个月
MOMENTUM_SKIP = 21  # 跳过最近 1 个月（12-1）
TREND_MA_SHORT = 20
TREND_MA_LONG = 60
LOWVOL_WINDOW = 60
MIN_HISTORY_DAYS = 253  # 入选打分所需的最少历史 bar 数（覆盖 T 至 T-252）

WINSOR_LOWER_Q = 0.01
WINSOR_UPPER_Q = 0.99
WINSOR_MIN_SAMPLES = 8  # 行业内样本不足时不做 winsorize，直接 z-score
INDUSTRY_MIN_SAMPLES = 2  # 行业内 z-score 的最小样本
MIN_FAMILY_WEIGHT_COVERAGE = 0.75

MAX_FACTOR_ROWS = 500  # factors 接口返回行数上限（按复合分排序截断）


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StockContext:
    """一只股票在打分日的输入：历史 bar（≤T 升序）与 PIT 财务快照（≤T）。"""

    info: StockInfo
    bars: tuple[StockBar, ...]  # 打分日 T 及之前的 bar，按日期升序
    fundamentals: tuple[Fundamentals, ...]  # available_at ≤ T 的快照，按日期升序


@dataclass
class FactorResult:
    """一只股票的因子结果：原始值 → 行业内 z → 族分 → 复合分。"""

    code: str
    name: str
    industry: str
    raw: dict[str, float | None] = field(default_factory=dict)
    zscores: dict[str, float | None] = field(default_factory=dict)
    quality: float | None = None
    value: float | None = None
    momentum: float | None = None
    trend: float | None = None
    lowvol: float | None = None
    composite: float = 0.0
    data_coverage: float = 0.0
    eligible: bool = True
    market_cap: float | None = None
    float_market_cap: float | None = None
    size_exposure: float | None = None
    beta_exposure: float | None = None
    liquidity_exposure: float | None = None
    average_daily_amount: float | None = None
    data_warnings: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# 横截面工具：winsorize / z-score / 分位数
# ---------------------------------------------------------------------------


def _percentile(sorted_values: list[float], q: float) -> float:
    """线性插值分位数（type 7 口径），sorted_values 必须非空且升序。"""
    if len(sorted_values) == 1:
        return sorted_values[0]
    pos = q * (len(sorted_values) - 1)
    lower = int(pos)
    upper = min(lower + 1, len(sorted_values) - 1)
    frac = pos - lower
    return sorted_values[lower] * (1.0 - frac) + sorted_values[upper] * frac


def winsorize(
    values: dict[str, float | None],
    lower_q: float = WINSOR_LOWER_Q,
    upper_q: float = WINSOR_UPPER_Q,
    min_samples: int = WINSOR_MIN_SAMPLES,
) -> dict[str, float | None]:
    """横截面 winsorize：把有效值截断到 [q_lower, q_upper] 分位区间。

    有效样本 < min_samples 时原样返回（小样本分位数不稳定，容易误伤）；
    None（缺失）保持 None。
    """
    valid = sorted(v for v in values.values() if v is not None)
    if len(valid) < min_samples:
        return dict(values)
    lo = _percentile(valid, lower_q)
    hi = _percentile(valid, upper_q)
    return {
        key: (min(max(value, lo), hi) if value is not None else None)
        for key, value in values.items()
    }


def zscore(values: dict[str, float | None]) -> dict[str, float | None]:
    """横截面 z-score（总体标准差口径）。

    有效样本 <2 或标准差为 0 时，有效值一律记 0（无横截面区分度）；
    None（缺失）保持 None，由复合层按可用权重重新归一化。
    """
    valid = [v for v in values.values() if v is not None]
    if len(valid) < INDUSTRY_MIN_SAMPLES:
        return {key: (0.0 if v is not None else None) for key, v in values.items()}
    mean = fmean(valid)
    variance = sum((v - mean) ** 2 for v in valid) / len(valid)
    std = math.sqrt(variance)
    if std == 0:
        return {key: (0.0 if v is not None else None) for key, v in values.items()}
    return {key: ((v - mean) / std if v is not None else None) for key, v in values.items()}


def historical_percentile(series: list[float], value: float) -> float | None:
    """value 在自身历史序列中的分位数 ∈ [0,1]（小于该值的占比，并列计半）。"""
    valid = [v for v in series if v is not None]
    if not valid:
        return None
    below = sum(1 for v in valid if v < value)
    equal = sum(1 for v in valid if v == value)
    return (below + 0.5 * equal) / len(valid)


# ---------------------------------------------------------------------------
# 原始因子计算（单只股票，仅用 ≤T 数据）
# ---------------------------------------------------------------------------


def _latest_with_values(
    fundamentals: tuple[Fundamentals, ...],
    as_of: date,
    fields: tuple[str, ...],
) -> Fundamentals | None:
    """PIT：取截至 as_of 对指定字段至少有一个有效值的最新快照。

    财务报告和市场估值的可用日期不同，仓储会把它们作为独立快照传入；
    因此质量字段与估值字段不能简单共用“最后一条 Fundamentals”。
    """
    result: Fundamentals | None = None
    for snapshot in fundamentals:
        if snapshot.available_at <= as_of:
            if any(getattr(snapshot, field) is not None for field in fields):
                result = snapshot
        else:
            break
    return result


def _fundamental_value_series(
    fundamentals: tuple[Fundamentals, ...], as_of: date
) -> list[float]:
    """估值因子（EP+BP 均值）的 PIT 历史序列（仅 available_at ≤ as_of）。

    用于估值历史分位：当前估值在其自身历史（同一可用性约束）中的分位。
    """
    series: list[float] = []
    for snapshot in fundamentals:
        if snapshot.available_at > as_of:
            break
        values = [v for v in (snapshot.ep, snapshot.bp) if v is not None]
        if values:
            series.append(fmean(values))
    return series


def momentum_12_1(closes: list[float]) -> float | None:
    """12-1 动量：closes[-1-21] / closes[-1-252] - 1。

    closes 为打分日 T 截止的收盘价升序序列；T 本身是最后一个点，
    因此 T-252 对应索引 -253，最少需要 253 个点。最近 21 个交易日
    （T 至 T-20）不参与动量收益。
    """
    if len(closes) < MOMENTUM_WINDOW + 1:
        return None
    end_value = closes[-1 - MOMENTUM_SKIP]
    start_value = closes[-1 - MOMENTUM_WINDOW]
    if start_value <= 0:
        return None
    return end_value / start_value - 1.0


def trend_strength(closes: list[float]) -> float | None:
    """趋势确认 ∈ [-1, 1]：现价≥MA20、现价≥MA60、MA20≥MA60 各 ±1 归一化。"""
    if len(closes) < TREND_MA_SHORT:
        return None
    price = closes[-1]
    ma20 = fmean(closes[-TREND_MA_SHORT:])
    score = 1.0 if price >= ma20 else -1.0
    count = 1
    if len(closes) >= TREND_MA_LONG:
        ma60 = fmean(closes[-TREND_MA_LONG:])
        score += 1.0 if price >= ma60 else -1.0
        score += 1.0 if ma20 >= ma60 else -1.0
        count += 2
    return score / count


def low_volatility(closes: list[float], window: int = LOWVOL_WINDOW) -> float | None:
    """低波动因子 = 近 window 日日收益标准差的相反数（越大越好）。"""
    if len(closes) < window + 1:
        return None
    tail = closes[-window - 1 :]
    returns = [
        tail[i] / tail[i - 1] - 1.0 for i in range(1, len(tail)) if tail[i - 1] > 0
    ]
    if len(returns) < 2:
        return None
    mean = fmean(returns)
    variance = sum((r - mean) ** 2 for r in returns) / (len(returns) - 1)
    return -math.sqrt(variance)


def period_return(
    closes: list[float], window: int, skip: int = 0
) -> float | None:
    """固定交易日窗口收益，可选跳过尾部交易日。"""
    if len(closes) < window + 1:
        return None
    end_index = -1 - skip
    start_index = -1 - window
    if abs(end_index) > len(closes) or closes[start_index] <= 0:
        return None
    return closes[end_index] / closes[start_index] - 1.0


def maximum_drawdown_factor(closes: list[float], window: int = 120) -> float | None:
    """近 window 日最大回撤的相反数（越大越好）。"""
    if len(closes) < window:
        return None
    peak = closes[-window]
    worst = 0.0
    for value in closes[-window:]:
        peak = max(peak, value)
        if peak > 0:
            worst = min(worst, value / peak - 1.0)
    return worst


def _stability(
    snapshots: tuple[Fundamentals, ...], field_name: str
) -> float | None:
    values = [
        float(value)
        for snapshot in snapshots[-8:]
        if (value := getattr(snapshot, field_name)) is not None
    ]
    if len(values) < 3:
        return None
    mean = fmean(values)
    variance = sum((value - mean) ** 2 for value in values) / (len(values) - 1)
    return -math.sqrt(variance)


def raw_factors(context: StockContext, as_of: date) -> dict[str, float | None]:
    """计算单只股票的全部原始因子（未经横截面处理）。

    无未来数据：价格序列已在 context 中按 ≤T 截断；基本面按 available_at
    ≤ T 取最新快照，估值分位的历史窗口同样按 T 过滤。
    """
    closes = [bar.close for bar in context.bars if not bar.suspended and bar.close > 0]
    latest_quality = _latest_with_values(
        context.fundamentals,
        as_of,
        (
            "roe",
            "roa",
            "gross_margin",
            "net_margin",
            "ocf_to_profit",
            "debt_ratio",
            "net_income",
            "operating_cash_flow",
            "total_assets",
        ),
    )
    latest_value = _latest_with_values(
        context.fundamentals,
        as_of,
        ("ep", "bp", "market_cap", "sales_yield", "dividend_yield"),
    )
    finance = any(
        marker in context.info.industry
        for marker in ("银行", "证券", "保险", "多元金融")
    )

    result: dict[str, float | None] = {
        "momentum_12_1": momentum_12_1(closes),
        "momentum_6_1": period_return(closes, 126, 21),
        "short_reversal": (
            -value if (value := period_return(closes, 21)) is not None else None
        ),
        "residual_momentum": (
            period_return(closes, 252, 21) - period_return(closes, 126, 21)
            if period_return(closes, 252, 21) is not None
            and period_return(closes, 126, 21) is not None
            else None
        ),
        "trend": trend_strength(closes),
        "volatility_60": low_volatility(closes, 60),
        "volatility_120": low_volatility(closes, 120),
        "max_drawdown_120": maximum_drawdown_factor(closes, 120),
        "residual_volatility": None,
    }

    for name in ("roe", "roa", "gross_margin", "net_margin", "ocf_to_profit", "debt_ratio"):
        result[name] = getattr(latest_quality, name) if latest_quality else None
    accruals = None
    if (
        latest_quality is not None
        and latest_quality.net_income is not None
        and latest_quality.operating_cash_flow is not None
        and latest_quality.total_assets is not None
        and latest_quality.total_assets > 0
    ):
        accruals = (
            latest_quality.net_income - latest_quality.operating_cash_flow
        ) / latest_quality.total_assets
    result["accruals"] = accruals
    result["earnings_stability"] = _stability(context.fundamentals, "roe")
    margins = [
        snapshot.net_margin
        for snapshot in context.fundamentals[-5:]
        if snapshot.net_margin is not None
    ]
    result["margin_change"] = (
        margins[-1] - margins[-2] if len(margins) >= 2 else None
    )
    # 金融公司不用工业企业毛利率、现金流质量与负债率模型。
    result["financial_roe"] = result["roe"] if finance else None
    result["financial_roa"] = result["roa"] if finance else None
    result["financial_earnings_stability"] = (
        result["earnings_stability"] if finance else None
    )
    if finance:
        for name in (
            "gross_margin",
            "net_margin",
            "ocf_to_profit",
            "accruals",
            "margin_change",
            "debt_ratio",
        ):
            result[name] = None
    result["ep"] = latest_value.ep if latest_value is not None else None
    result["bp"] = latest_value.bp if latest_value is not None else None
    result["sales_yield"] = (
        latest_value.sales_yield if latest_value is not None else None
    )
    result["dividend_yield"] = (
        latest_value.dividend_yield if latest_value is not None else None
    )
    result["market_cap"] = (
        latest_value.market_cap if latest_value is not None else None
    )
    result["float_market_cap"] = (
        latest_value.float_market_cap if latest_value is not None else None
    )
    latest_fcf = _latest_with_values(
        context.fundamentals, as_of, ("free_cash_flow",)
    )
    result["fcf_yield"] = (
        latest_fcf.free_cash_flow / latest_value.market_cap
        if latest_fcf is not None
        and latest_fcf.free_cash_flow is not None
        and latest_value is not None
        and latest_value.market_cap is not None
        and latest_value.market_cap > 0
        else None
    )
    latest_profit = _latest_with_values(
        context.fundamentals, as_of, ("net_income",)
    )
    result["loss_profitability"] = (
        -1.0
        if latest_profit is not None
        and latest_profit.net_income is not None
        and latest_profit.net_income <= 0
        else 0.0
        if latest_profit is not None and latest_profit.net_income is not None
        else None
    )

    current_values = [
        v for v in (result["ep"], result["bp"]) if v is not None
    ]
    if current_values:
        current = fmean(current_values)
        history = _fundamental_value_series(context.fundamentals, as_of)
        result["valuation_percentile"] = historical_percentile(history, current)
    else:
        result["valuation_percentile"] = None
    return result


# ---------------------------------------------------------------------------
# 行业内横截面处理与复合
# ---------------------------------------------------------------------------

# 原始因子 → (所属族, 方向)
_FACTOR_FAMILY: dict[str, str] = {
    **{name: "quality" for name in QUALITY_DIRECTIONS},
    **{name: "value" for name in VALUE_DIRECTIONS},
    "momentum_12_1": "momentum",
    "momentum_6_1": "momentum",
    "short_reversal": "momentum",
    "residual_momentum": "momentum",
    "trend": "trend",
    "volatility_60": "lowvol",
    "volatility_120": "lowvol",
    "max_drawdown_120": "lowvol",
    "residual_volatility": "lowvol",
}
_FACTOR_DIRECTION: dict[str, int] = {
    **QUALITY_DIRECTIONS,
    **VALUE_DIRECTIONS,
    "momentum_12_1": 1,
    "momentum_6_1": 1,
    "short_reversal": 1,
    "residual_momentum": 1,
    "trend": 1,
    "volatility_60": 1,
    "volatility_120": 1,
    "max_drawdown_120": 1,
    "residual_volatility": 1,
}


def _combine_family(zvalues: dict[str, float | None], family: str) -> float | None:
    """族内子因子 z 值等权复合；缺失剔除后重新归一，全部缺失返回 None。"""
    names = [name for name, fam in _FACTOR_FAMILY.items() if fam == family]
    valid = [zvalues[name] for name in names if zvalues.get(name) is not None]
    if not valid:
        return None
    return fmean(valid)  # type: ignore[arg-type]


def compute_cross_section(
    contexts: list[StockContext],
    as_of: date,
    weights: dict[str, float] | None = None,
) -> list[FactorResult]:
    """横截面打分：原始因子 → 行业内 winsorize+z → 族分 → 加权复合分。

    复合分 = Σ 族权重 × 族分，缺失族按可用族权重重新归一化；
    全部族缺失时复合分为 0 并记录 warning。weights 缺省为
    DEFAULT_FAMILY_WEIGHTS（30/25/20/15/10），不修改任何全局状态。
    """
    family_weights = dict(DEFAULT_FAMILY_WEIGHTS)
    if weights:
        for family, value in weights.items():
            if family in family_weights and value is not None and value >= 0:
                family_weights[family] = float(value)

    # 用当期 universe 等权收益作为市场代理，估算无未来数据的120日 Beta。
    returns_by_code: dict[str, dict[date, float]] = {}
    market_by_date: dict[date, list[float]] = {}
    for context in contexts:
        usable = [bar for bar in context.bars if not bar.suspended and bar.close > 0]
        returns: dict[date, float] = {}
        for index in range(1, len(usable)):
            previous = usable[index - 1].close
            if previous > 0:
                value = usable[index].close / previous - 1.0
                returns[usable[index].trade_date] = value
                market_by_date.setdefault(usable[index].trade_date, []).append(value)
        returns_by_code[context.info.code] = returns
    market_returns = {
        day: fmean(values) for day, values in market_by_date.items() if values
    }

    results: list[FactorResult] = []
    for context in contexts:
        raw = raw_factors(context, as_of)
        aligned = [
            (value, market_returns[day])
            for day, value in sorted(returns_by_code[context.info.code].items())
            if day in market_returns
        ]
        pairs = aligned[-120:]
        beta = None
        alpha = 0.0
        if len(pairs) >= 20:
            market_mean = fmean(pair[1] for pair in pairs)
            stock_mean = fmean(pair[0] for pair in pairs)
            variance = sum((pair[1] - market_mean) ** 2 for pair in pairs)
            if variance > 0:
                beta = sum(
                    (stock - stock_mean) * (market - market_mean)
                    for stock, market in pairs
                ) / variance
                alpha = stock_mean - beta * market_mean
        residual_volatility = None
        if beta is not None and len(pairs) >= 20:
            residuals = [
                stock - alpha - beta * market for stock, market in pairs
            ]
            residual_mean = fmean(residuals)
            residual_volatility = -math.sqrt(
                sum((value - residual_mean) ** 2 for value in residuals)
                / (len(residuals) - 1)
            )
        # 12-1 残差动量：在 252 日对齐窗口内跳过最近 21 日，用市场模型
        # alpha/beta 的日残差复合，避免把单纯高 Beta 上涨误当个股动量。
        momentum_pairs = aligned[-252:-21] if len(aligned) >= 253 else []
        residual_momentum = None
        if len(momentum_pairs) >= 126:
            market_mean = fmean(pair[1] for pair in momentum_pairs)
            stock_mean = fmean(pair[0] for pair in momentum_pairs)
            market_variance = sum(
                (pair[1] - market_mean) ** 2 for pair in momentum_pairs
            )
            if market_variance > 0:
                momentum_beta = sum(
                    (stock - stock_mean) * (market - market_mean)
                    for stock, market in momentum_pairs
                ) / market_variance
                momentum_alpha = stock_mean - momentum_beta * market_mean
                compounded = 1.0
                for stock, market in momentum_pairs:
                    compounded *= 1.0 + (
                        stock - momentum_alpha - momentum_beta * market
                    )
                residual_momentum = compounded - 1.0
        recent_amounts = [
            bar.amount
            for bar in context.bars[-20:]
            if bar.amount is not None and bar.amount > 0
        ]
        cap = raw.get("float_market_cap") or raw.get("market_cap")
        size = math.log(cap) if cap is not None and cap > 0 else None
        liquidity = (
            math.log(fmean(recent_amounts)) if recent_amounts else None
        )
        average_daily_amount = fmean(recent_amounts) if recent_amounts else None
        raw["beta"] = beta
        raw["residual_volatility"] = residual_volatility
        raw["residual_momentum"] = residual_momentum
        raw["size"] = size
        raw["liquidity"] = liquidity
        raw["average_daily_amount"] = average_daily_amount
        results.append(
            FactorResult(
                code=context.info.code,
                name=context.info.name,
                industry=context.info.industry or "未知",
                raw=raw,
                market_cap=raw.get("market_cap"),
                float_market_cap=raw.get("float_market_cap"),
                size_exposure=size,
                beta_exposure=beta,
                liquidity_exposure=liquidity,
                average_daily_amount=average_daily_amount,
            )
        )

    # 按行业分组做 winsorize + z-score（行业中性化：每只股票只与同行业比较）
    by_industry: dict[str, list[FactorResult]] = {}
    for result in results:
        by_industry.setdefault(result.industry, []).append(result)

    for members in by_industry.values():
        for factor_name, direction in _FACTOR_DIRECTION.items():
            column = {member.code: member.raw.get(factor_name) for member in members}
            # 方向调整：负债率等「越小越好」的因子取负后参与 winsorize/z
            oriented = {
                code: (value * direction if value is not None else None)
                for code, value in column.items()
            }
            zscores = zscore(winsorize(oriented))
            for member in members:
                member.zscores[factor_name] = zscores[member.code]
        # 趋势对 12-1 动量做行业内横截面正交化，族权重不再重复押注同一
        # 排名方向；样本不足或动量无方差时保留原趋势分。
        momentum_trend = [
            (
                member,
                member.zscores.get("momentum_12_1"),
                member.zscores.get("trend"),
            )
            for member in members
            if member.zscores.get("momentum_12_1") is not None
            and member.zscores.get("trend") is not None
        ]
        if len(momentum_trend) >= 3:
            momentum_mean = fmean(float(item[1]) for item in momentum_trend)
            trend_mean = fmean(float(item[2]) for item in momentum_trend)
            variance = sum(
                (float(momentum) - momentum_mean) ** 2
                for _member, momentum, _trend in momentum_trend
            )
            if variance > 0:
                slope = sum(
                    (float(momentum) - momentum_mean)
                    * (float(trend) - trend_mean)
                    for _member, momentum, trend in momentum_trend
                ) / variance
                for member, momentum, trend in momentum_trend:
                    member.zscores["trend"] = (
                        float(trend)
                        - trend_mean
                        - slope * (float(momentum) - momentum_mean)
                    )

    for result in results:
        result.quality = _combine_family(result.zscores, "quality")
        result.value = _combine_family(result.zscores, "value")
        result.momentum = _combine_family(result.zscores, "momentum")
        result.trend = _combine_family(result.zscores, "trend")
        result.lowvol = _combine_family(result.zscores, "lowvol")

        weighted = 0.0
        weight_sum = 0.0
        total_weight = sum(family_weights.values())
        for family, weight in family_weights.items():
            family_score = getattr(result, family)
            if family_score is None:
                continue
            weighted += weight * family_score
            weight_sum += weight
        if weight_sum <= 0:
            result.composite = 0.0
            result.data_coverage = 0.0
            result.eligible = False
            result.data_warnings.append("全部因子族缺失，复合分按 0 处理")
        else:
            result.composite = weighted / weight_sum
            result.data_coverage = (
                weight_sum / total_weight if total_weight > 0 else 0.0
            )
            result.eligible = (
                result.data_coverage + 1e-12 >= MIN_FAMILY_WEIGHT_COVERAGE
            )
            if weight_sum < total_weight - 1e-9:
                missing = [
                    family
                    for family in family_weights
                    if getattr(result, family) is None
                ]
                result.data_warnings.append(
                    f"因子族缺失（{'/'.join(missing)}），复合分按可用族权重归一化"
                )
            if not result.eligible:
                result.data_warnings.append(
                    f"可用因子权重 {result.data_coverage:.0%} 低于入选门槛 "
                    f"{MIN_FAMILY_WEIGHT_COVERAGE:.0%}，仅展示不参与组合"
                )
    return results


# ---------------------------------------------------------------------------
# 上下文构造（服务层/回测共用）
# ---------------------------------------------------------------------------


def build_context(
    info: StockInfo,
    bars: list[StockBar],
    fundamentals: list[Fundamentals],
    as_of: date,
) -> StockContext:
    """按打分日截断并排序一只股票的历史数据（无未来数据的最后一道闸）。"""
    usable_bars = sorted(
        (bar for bar in bars if bar.trade_date <= as_of),
        key=lambda bar: bar.trade_date,
    )
    usable_fundamentals = sorted(
        (snap for snap in fundamentals if snap.available_at <= as_of),
        key=lambda snap: snap.available_at,
    )
    return StockContext(
        info=info,
        bars=tuple(usable_bars),
        fundamentals=tuple(usable_fundamentals),
    )


def history_depth(context: StockContext) -> int:
    """有效历史 bar 数（非停牌、正收盘），用于样本充足性判断。"""
    return sum(1 for bar in context.bars if not bar.suspended and bar.close > 0)


def factor_correlation_matrix(
    results: list[FactorResult],
) -> dict[str, dict[str, float | None]]:
    """当期因子族分的 Pearson 相关矩阵，用于识别重复暴露。"""
    families = ("quality", "value", "momentum", "trend", "lowvol")
    matrix: dict[str, dict[str, float | None]] = {}
    for left in families:
        matrix[left] = {}
        for right in families:
            pairs = [
                (float(a), float(b))
                for item in results
                if (a := getattr(item, left)) is not None
                and (b := getattr(item, right)) is not None
            ]
            if len(pairs) < 3:
                matrix[left][right] = None
                continue
            left_mean = fmean(a for a, _b in pairs)
            right_mean = fmean(b for _a, b in pairs)
            numerator = sum(
                (a - left_mean) * (b - right_mean) for a, b in pairs
            )
            denominator = math.sqrt(
                sum((a - left_mean) ** 2 for a, _b in pairs)
                * sum((b - right_mean) ** 2 for _a, b in pairs)
            )
            matrix[left][right] = (
                numerator / denominator if denominator > 0 else None
            )
    return matrix


__all__ = [
    "DEFAULT_FAMILY_WEIGHTS",
    "FactorResult",
    "MAX_FACTOR_ROWS",
    "MIN_HISTORY_DAYS",
    "StockContext",
    "build_context",
    "compute_cross_section",
    "factor_correlation_matrix",
    "historical_percentile",
    "history_depth",
    "low_volatility",
    "momentum_12_1",
    "raw_factors",
    "trend_strength",
    "winsorize",
    "zscore",
]
