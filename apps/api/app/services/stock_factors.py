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
from dataclasses import dataclass, field, replace
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
    "cash_conversion_assets": 1,
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
from app.services.financial_sector_model import (  # noqa: E402
    factor_directions as _financial_directions,
    factor_families as _financial_families,
)

for _financial_name, _financial_direction in _financial_directions().items():
    if _financial_families()[_financial_name] == "quality":
        QUALITY_DIRECTIONS[_financial_name] = _financial_direction
    else:
        VALUE_DIRECTIONS[_financial_name] = _financial_direction

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
    factor_metadata: dict[str, dict[str, object]] = field(default_factory=dict)
    model_structure: dict[str, object] = field(default_factory=dict)
    model_eligible: bool = True


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
    return {
        key: ((v - mean) / std if v is not None else None) for key, v in values.items()
    }


MIN_VALUATION_HISTORY_OBSERVATIONS = 24


def historical_percentile(
    series: list[float],
    value: float,
    *,
    minimum_observations: int = MIN_VALUATION_HISTORY_OBSERVATIONS,
) -> float | None:
    """value 在自身历史序列中的分位数 ∈ [0,1]（小于该值的占比，并列计半）。"""
    valid = [v for v in series if v is not None]
    if len(valid) < minimum_observations:
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
            if not snapshot.formal_factor_usable:
                continue
            if any(getattr(snapshot, field) is not None for field in fields):
                result = snapshot
        else:
            break
    return result


def _latest_sector_snapshot(
    fundamentals: tuple[Fundamentals, ...],
    as_of: date,
    fields: tuple[str, ...],
) -> Fundamentals | None:
    """按字段取得截至 T 的最新金融专用指标，形成可审计的 as-of 快照。

    银行监管指标的披露频率并不一致：不良率、净息差常按季披露，而资本
    充足率可能只在半年报/年报出现。若直接取“包含任一字段的最新报告”，
    新一季报会把上一期仍然有效的资本充足率覆盖成空值，进而把整个银行
    行业错误剔除。这里对每个字段分别做 last-observation-carried-forward，
    但只允许使用 ``available_at <= as_of`` 的正式可用记录，不跨越信号日。
    """
    latest: Fundamentals | None = None
    values: dict[str, object] = {}
    sources: set[str] = set()
    for snapshot in fundamentals:
        if snapshot.available_at > as_of:
            break
        if not snapshot.formal_factor_usable:
            continue
        observed = False
        for field_name in fields:
            value = getattr(snapshot, field_name)
            if value is not None:
                values[field_name] = value
                observed = True
        if observed:
            latest = snapshot
            sources.update(snapshot.sector_metric_sources)
    if latest is None:
        return None
    return replace(
        latest,
        **values,
        sector_metric_sources=tuple(sorted(sources)),
    )


def _fundamental_value_series(
    fundamentals: tuple[Fundamentals, ...], as_of: date
) -> list[float]:
    """估值因子（EP+BP 均值）的 PIT 历史序列（仅 available_at ≤ as_of）。

    用于估值历史分位：当前估值在其自身历史（同一可用性约束）中的分位。
    """
    observations: dict[date, float] = {}
    for snapshot in fundamentals:
        if snapshot.available_at > as_of:
            break
        values = [v for v in (snapshot.ep, snapshot.bp) if v is not None]
        if values:
            observations[snapshot.valuation_date or snapshot.available_at] = fmean(
                values
            )
    return [observations[day] for day in sorted(observations)]


def _scaled_window(window: int, scale: float) -> int:
    if not math.isfinite(scale) or scale <= 0:
        raise ValueError("factor window scale must be finite and positive")
    return max(2, round(window * scale))


def minimum_history_days(window_scale: float = 1.0) -> int:
    """返回价格因子所需的最长历史，供股票池和计算层共用。"""
    return _scaled_window(MOMENTUM_WINDOW, window_scale) + 1


def residual_momentum_from_returns(
    residual_values: list[float],
    *,
    window_scale: float = 1.0,
) -> float | None:
    """从收益残差计算 12-1 动量。

    253 个价格点只会产生 252 个收益点，因此成熟条件必须按收益窗口
    判断，不能错误复用价格历史深度 253。
    """
    return_window = _scaled_window(MOMENTUM_WINDOW, window_scale)
    skip_window = _scaled_window(MOMENTUM_SKIP, window_scale)
    minimum_compound_window = _scaled_window(126, window_scale)
    if len(residual_values) < return_window:
        return None
    momentum_residuals = residual_values[-return_window:-skip_window]
    if len(momentum_residuals) < minimum_compound_window:
        return None
    compounded = 1.0
    for value in momentum_residuals:
        compounded *= 1.0 + value
    return compounded - 1.0


def momentum_12_1(
    closes: list[float],
    *,
    window: int = MOMENTUM_WINDOW,
    skip: int = MOMENTUM_SKIP,
) -> float | None:
    """12-1 动量：closes[-1-21] / closes[-1-252] - 1。

    closes 为打分日 T 截止的收盘价升序序列；T 本身是最后一个点，
    因此 T-252 对应索引 -253，最少需要 253 个点。最近 21 个交易日
    （T 至 T-20）不参与动量收益。
    """
    if len(closes) < window + 1 or window <= skip:
        return None
    end_value = closes[-1 - skip]
    start_value = closes[-1 - window]
    if start_value <= 0:
        return None
    return end_value / start_value - 1.0


def trend_strength(
    closes: list[float],
    *,
    short_window: int = TREND_MA_SHORT,
    long_window: int = TREND_MA_LONG,
) -> float | None:
    """趋势确认 ∈ [-1, 1]：现价≥MA20、现价≥MA60、MA20≥MA60 各 ±1 归一化。"""
    if len(closes) < short_window:
        return None
    price = closes[-1]
    ma20 = fmean(closes[-short_window:])
    score = 1.0 if price >= ma20 else -1.0
    count = 1
    if len(closes) >= long_window:
        ma60 = fmean(closes[-long_window:])
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


def period_return(closes: list[float], window: int, skip: int = 0) -> float | None:
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


def _stability(snapshots: tuple[Fundamentals, ...], field_name: str) -> float | None:
    values = [
        float(value)
        for snapshot in snapshots[-8:]
        if snapshot.formal_factor_usable
        if (value := getattr(snapshot, field_name)) is not None
    ]
    if len(values) < 3:
        return None
    mean = fmean(values)
    variance = sum((value - mean) ** 2 for value in values) / (len(values) - 1)
    return -math.sqrt(variance)


def _financial_policy_assessments(
    context: StockContext,
    as_of: date,
):
    from app.services.financial_ratio_policy import (
        assess_cash_conversion,
        assess_fcf_yield,
    )

    latest_quality = _latest_with_values(
        context.fundamentals,
        as_of,
        ("net_income", "operating_cash_flow", "total_assets"),
    )
    latest_value = _latest_with_values(context.fundamentals, as_of, ("market_cap",))
    latest_fcf = _latest_with_values(context.fundamentals, as_of, ("free_cash_flow",))
    finance = any(
        marker in context.info.industry
        for marker in ("银行", "证券", "保险", "多元金融")
    )
    cash = assess_cash_conversion(
        net_income=latest_quality.net_income if latest_quality else None,
        operating_cash_flow=(
            latest_quality.operating_cash_flow if latest_quality else None
        ),
        total_assets=latest_quality.total_assets if latest_quality else None,
    )
    fcf = assess_fcf_yield(
        free_cash_flow=(latest_fcf.free_cash_flow if latest_fcf is not None else None),
        flow_basis=latest_fcf.flow_basis if latest_fcf is not None else None,
        market_cap=latest_value.market_cap if latest_value is not None else None,
        market_cap_date=(
            latest_value.valuation_date if latest_value is not None else None
        ),
        signal_date=as_of,
        is_financial_company=finance,
        unit_policy=latest_fcf.unit_policy if latest_fcf is not None else None,
    )
    return cash, fcf


def raw_factors(
    context: StockContext,
    as_of: date,
    *,
    window_scale: float = 1.0,
) -> dict[str, float | None]:
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
    sector_fields = tuple(
        name for name in _financial_directions() if not name.endswith(("_ep", "_bp"))
    )
    latest_sector = _latest_sector_snapshot(
        context.fundamentals,
        as_of,
        sector_fields + ("company_type",),
    )
    from app.services.financial_sector_model import assess_financial_sector

    sector_assessment = (
        assess_financial_sector(
            latest_sector,
            industry=context.info.industry,
            valuation=latest_value,
        )
        if latest_sector is not None
        else None
    )
    finance = any(
        marker in context.info.industry
        for marker in ("银行", "证券", "保险", "多元金融")
    )

    momentum_window = _scaled_window(252, window_scale)
    medium_window = _scaled_window(126, window_scale)
    skip_window = _scaled_window(21, window_scale)
    reversal_window = _scaled_window(21, window_scale)
    trend_short = _scaled_window(20, window_scale)
    trend_long = _scaled_window(60, window_scale)
    volatility_short = _scaled_window(60, window_scale)
    volatility_long = _scaled_window(120, window_scale)
    drawdown_window = _scaled_window(120, window_scale)
    long_momentum = period_return(closes, momentum_window, skip_window)
    medium_momentum = period_return(closes, medium_window, skip_window)
    result: dict[str, float | None] = {
        "momentum_12_1": momentum_12_1(
            closes,
            window=momentum_window,
            skip=skip_window,
        ),
        "momentum_6_1": medium_momentum,
        "short_reversal": (
            -value
            if (value := period_return(closes, reversal_window)) is not None
            else None
        ),
        "residual_momentum": (
            long_momentum - medium_momentum
            if long_momentum is not None and medium_momentum is not None
            else None
        ),
        "trend": trend_strength(
            closes,
            short_window=trend_short,
            long_window=trend_long,
        ),
        "volatility_60": low_volatility(closes, volatility_short),
        "volatility_120": low_volatility(closes, volatility_long),
        "max_drawdown_120": maximum_drawdown_factor(closes, drawdown_window),
        "residual_volatility": None,
    }
    result.update({field: None for field in sector_fields})
    if sector_assessment is not None:
        result.update(sector_assessment.values)

    for name in ("roe", "roa", "gross_margin", "net_margin", "debt_ratio"):
        result[name] = getattr(latest_quality, name) if latest_quality else None
    cash_assessment, fcf_assessment = _financial_policy_assessments(context, as_of)
    result["ocf_to_profit"] = cash_assessment.ocf_to_profit
    result["cash_conversion_assets"] = cash_assessment.cash_conversion_assets
    result["accruals"] = (
        -cash_assessment.cash_conversion_assets
        if cash_assessment.cash_conversion_assets is not None
        else None
    )
    result["earnings_stability"] = _stability(context.fundamentals, "roe")
    margins = [
        snapshot.net_margin
        for snapshot in context.fundamentals[-5:]
        if snapshot.net_margin is not None
    ]
    result["margin_change"] = margins[-1] - margins[-2] if len(margins) >= 2 else None
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
            "cash_conversion_assets",
            "accruals",
            "margin_change",
            "debt_ratio",
        ):
            result[name] = None
    if sector_assessment is not None:
        # 正式金融类型只进入自己的质量/风险/价值字段，通用金融三因子与
        # 工业 EP/BP 不得并行补位。
        for name in (
            "financial_roe",
            "financial_roa",
            "financial_earnings_stability",
        ):
            result[name] = None
    result["ep"] = latest_value.ep if latest_value is not None else None
    result["bp"] = latest_value.bp if latest_value is not None else None
    if sector_assessment is not None:
        result["ep"] = None
        result["bp"] = None
    result["sales_yield"] = (
        latest_value.sales_yield if latest_value is not None else None
    )
    result["dividend_yield"] = (
        latest_value.dividend_yield if latest_value is not None else None
    )
    result["market_cap"] = latest_value.market_cap if latest_value is not None else None
    result["float_market_cap"] = (
        latest_value.float_market_cap if latest_value is not None else None
    )
    result["fcf_yield"] = fcf_assessment.value
    latest_profit = _latest_with_values(context.fundamentals, as_of, ("net_income",))
    result["loss_profitability"] = (
        -1.0
        if latest_profit is not None
        and latest_profit.net_income is not None
        and latest_profit.net_income <= 0
        else 0.0
        if latest_profit is not None and latest_profit.net_income is not None
        else None
    )

    current_values = [v for v in (result["ep"], result["bp"]) if v is not None]
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


MISSING_OPTIONAL_PENALTY = -0.25


def _family_policy(result: FactorResult, family: str) -> tuple[list[str], set[str]]:
    """返回固定字段集合与必需项；集合不随单只股票的实际缺失而变化。"""
    sector = str(result.model_structure.get("sector") or "")
    # 金融专用契约只替换财务质量/价值族；动量、趋势和低波仍然来自所有
    # 股票共有的价格序列。此前这里对五个因子族无条件返回专用字段，
    # 导致后三个族拿到空字段集，完整银行模型也只有 55% 权重覆盖，
    # 从而整个银行业被 75% 覆盖门禁错误剔除。
    if sector in {"bank", "broker", "insurance"} and family in {
        "quality",
        "value",
    }:
        from app.services.financial_sector_model import FEATURE_DICTIONARIES

        features = [
            feature
            for feature in FEATURE_DICTIONARIES[sector]
            if ("quality" if feature.family == "risk" else feature.family) == family
        ]
        return (
            [feature.field for feature in features],
            {feature.field for feature in features if feature.required},
        )
    if family == "quality":
        if any(
            marker in result.industry for marker in ("银行", "证券", "保险", "多元金融")
        ):
            return (
                [
                    "financial_roe",
                    "financial_roa",
                    "financial_earnings_stability",
                ],
                {"financial_roe"},
            )
        return (
            [
                "roe",
                "roa",
                "gross_margin",
                "net_margin",
                "cash_conversion_assets",
                "accruals",
                "earnings_stability",
                "margin_change",
                "debt_ratio",
            ],
            {"roe"},
        )
    policies = {
        "value": (
            [
                "ep",
                "bp",
                "sales_yield",
                "dividend_yield",
                "fcf_yield",
                "loss_profitability",
                "valuation_percentile",
            ],
            {"bp"},
        ),
        "momentum": (
            [
                "momentum_12_1",
                "momentum_6_1",
                "short_reversal",
                "residual_momentum",
            ],
            {"momentum_12_1"},
        ),
        "trend": (["trend"], {"trend"}),
        "lowvol": (
            [
                "volatility_60",
                "volatility_120",
                "max_drawdown_120",
                "residual_volatility",
            ],
            {"volatility_60"},
        ),
    }
    names, required = policies[family]
    return list(names), set(required)


def _combine_family(
    result: FactorResult,
    family: str,
) -> tuple[float | None, dict[str, object]]:
    """固定权重复合；必需项缺失阻断，可选项按预注册惩罚填充。"""
    names, required = _family_policy(result, family)
    missing_required = sorted(
        name for name in required if result.zscores.get(name) is None
    )
    available = sorted(name for name in names if result.zscores.get(name) is not None)
    missing_optional = sorted(
        name
        for name in names
        if name not in required and result.zscores.get(name) is None
    )
    weight = 1.0 / len(names) if names else 0.0
    structure: dict[str, object] = {
        "fields": names,
        "required": sorted(required),
        "available": available,
        "missing_required": missing_required,
        "missing_optional": missing_optional,
        "missing_policy": (
            f"required_block;optional_fixed_penalty={MISSING_OPTIONAL_PENALTY}"
        ),
        "effective_weights": {name: weight for name in names},
    }
    if not names or missing_required:
        structure["status"] = "blocked_required_missing"
        return None, structure
    values = [
        (
            float(result.zscores[name])
            if result.zscores.get(name) is not None
            else MISSING_OPTIONAL_PENALTY
        )
        for name in names
    ]
    structure["status"] = "valid"
    return sum(weight * value for value in values), structure


def compute_cross_section(
    contexts: list[StockContext],
    as_of: date,
    weights: dict[str, float] | None = None,
    official_market_returns: dict[date, float] | None = None,
    window_scale: float = 1.0,
    winsor_quantiles: tuple[float, float] = (0.01, 0.99),
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
    from app.services.factor_residual_model import (
        build_return_proxy_aggregate,
        estimate_residual_model,
        leave_one_out_from_aggregate,
    )

    market_proxy_aggregate = build_return_proxy_aggregate(returns_by_code)
    industry_proxy_aggregates = {
        industry: build_return_proxy_aggregate(
            returns_by_code,
            eligible_codes={
                item.info.code for item in contexts if item.info.industry == industry
            },
        )
        for industry in {item.info.industry for item in contexts}
    }
    results: list[FactorResult] = []
    for context in contexts:
        raw = raw_factors(context, as_of, window_scale=window_scale)
        cash_assessment, fcf_assessment = _financial_policy_assessments(context, as_of)
        from app.services.financial_sector_model import assess_financial_sector

        latest_sector = _latest_sector_snapshot(
            context.fundamentals,
            as_of,
            tuple(
                name
                for name in _financial_directions()
                if not name.endswith(("_ep", "_bp"))
            )
            + ("company_type",),
        )
        latest_valuation = _latest_with_values(
            context.fundamentals, as_of, ("ep", "bp")
        )
        sector_assessment = (
            assess_financial_sector(
                latest_sector,
                industry=context.info.industry,
                valuation=latest_valuation,
            )
            if latest_sector is not None
            else None
        )
        own_returns = returns_by_code[context.info.code]
        if (
            official_market_returns
            and len(set(own_returns) & set(official_market_returns)) >= 60
        ):
            market_proxy = official_market_returns
            market_source = "official_total_return_index"
        else:
            market_proxy = leave_one_out_from_aggregate(
                market_proxy_aggregate,
                context.info.code,
                own_returns,
            )
            market_source = "leave_one_out_investable_universe"
        industry_proxy = leave_one_out_from_aggregate(
            industry_proxy_aggregates[context.info.industry],
            context.info.code,
            own_returns,
        )
        residual_model = estimate_residual_model(
            own_returns,
            market_proxy,
            market_source=market_source,
            industry_returns=industry_proxy or None,
        )
        beta = residual_model.beta
        residual_values = [
            value for _day, value in sorted(residual_model.residuals.items())
        ]
        residual_volatility = None
        recent_residuals = residual_values[-_scaled_window(120, window_scale) :]
        if len(recent_residuals) >= 20:
            residual_mean = fmean(recent_residuals)
            residual_volatility = -math.sqrt(
                sum((value - residual_mean) ** 2 for value in recent_residuals)
                / (len(recent_residuals) - 1)
            )
        # 12-1 残差动量直接复合已保存的模型残差，跳过最近21个交易日。
        residual_momentum = residual_momentum_from_returns(
            residual_values,
            window_scale=window_scale,
        )
        recent_amounts = [
            bar.amount
            for bar in context.bars[-20:]
            if bar.amount is not None and bar.amount > 0
        ]
        cap = raw.get("float_market_cap") or raw.get("market_cap")
        size = math.log(cap) if cap is not None and cap > 0 else None
        liquidity = math.log(fmean(recent_amounts)) if recent_amounts else None
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
                factor_metadata={
                    "cash_conversion": {
                        "profit_classification": (
                            cash_assessment.profit_classification
                        ),
                        "denominator_floor": cash_assessment.denominator_floor,
                        "issues": list(cash_assessment.issues),
                    },
                    "fcf_yield": {
                        "status": fcf_assessment.status,
                        "reason": fcf_assessment.reason,
                        **fcf_assessment.lineage,
                    },
                    "market_model": {
                        "model": residual_model.model,
                        "market_source": residual_model.market_source,
                        "window": residual_model.window,
                        "observations": residual_model.observations,
                        "missing_dates": residual_model.missing_dates,
                        "alpha": residual_model.alpha,
                        "beta": residual_model.beta,
                        "industry_beta": residual_model.industry_beta,
                        "r_squared": residual_model.r_squared,
                        "capm_r_squared": residual_model.capm_r_squared,
                    },
                },
                model_eligible=(
                    sector_assessment.eligible
                    if sector_assessment is not None
                    else True
                ),
                model_structure=(
                    {
                        "sector": sector_assessment.sector,
                        "eligible": sector_assessment.eligible,
                        "used_features": list(sector_assessment.used_features),
                        "missing_required": list(sector_assessment.missing_required),
                        "reason": sector_assessment.reason,
                    }
                    if sector_assessment is not None
                    else {"sector": "industrial_or_legacy", "eligible": True}
                ),
            )
        )
        if sector_assessment is not None and not sector_assessment.eligible:
            results[-1].data_warnings.append(sector_assessment.reason)

    # 全市场当期无区分度的字段先阻断，再进入行业横截面处理。尤其是
    # valuation_percentile：单点或重复快照不能以全体 0.5 伪装成有效因子。
    from app.services.factor_health import inspect_factor

    blocked_factors: dict[str, tuple[str, ...]] = {}
    # 此处只在评分内部阻断已知会由单点历史机械产生 0.5 的估值分位；
    # 全因子分布健康门禁由正式运行的 factor_health 报告统一判定，避免
    # 小型诊断股票池把“股票池本身同质”误判成源字段损坏。
    for factor_name in ("valuation_percentile",):
        health = inspect_factor(
            factor_name,
            [item.raw.get(factor_name) for item in results],
        )
        if health.blocked:
            blocked_factors[factor_name] = health.reasons
    for result in results:
        for factor_name, reasons in blocked_factors.items():
            if result.raw.get(factor_name) is not None:
                result.data_warnings.append(
                    f"{factor_name} 当期分布阻断：{'；'.join(reasons)}"
                )

    # 正式横截面使用行业哑变量 + log市值 + Beta + 流动性 WLS 残差化。
    # 小诊断股票池样本不足时才保留原行业内标准化回退。
    by_industry: dict[str, list[FactorResult]] = {}
    for result in results:
        by_industry.setdefault(result.industry, []).append(result)

    from app.services.cross_sectional_neutralization import (
        NeutralizationObservation,
        neutralize_wls,
    )

    for factor_name, direction in _FACTOR_DIRECTION.items():
        if factor_name in blocked_factors:
            for item in results:
                item.zscores[factor_name] = None
                item.factor_metadata.setdefault(factor_name, {})["health_block"] = list(
                    blocked_factors[factor_name]
                )
            continue
        oriented = {
            item.code: (
                item.raw.get(factor_name) * direction
                if item.raw.get(factor_name) is not None
                else None
            )
            for item in results
        }
        neutralization = neutralize_wls(
            [
                NeutralizationObservation(
                    code=item.code,
                    industry=item.industry,
                    value=oriented[item.code],
                    log_market_cap=item.size_exposure,
                    beta=item.beta_exposure,
                    liquidity=item.liquidity_exposure,
                    float_market_cap=item.float_market_cap,
                )
                for item in results
            ]
        )
        diagnostics = {
            "method": neutralization.method,
            "sample_size": neutralization.sample_size,
            "coefficients": neutralization.coefficients,
            "r_squared": neutralization.r_squared,
            "weighted_control_correlations": (
                neutralization.weighted_control_correlations
            ),
            "small_industries": list(neutralization.small_industries),
        }
        if neutralization.method == "wls_sqrt_float_market_cap":
            standardized = zscore(
                winsorize(
                    neutralization.residuals,
                    lower_q=winsor_quantiles[0],
                    upper_q=winsor_quantiles[1],
                )
            )
            for item in results:
                item.zscores[factor_name] = standardized[item.code]
                item.factor_metadata.setdefault(factor_name, {})["neutralization"] = (
                    diagnostics
                )
        else:
            for members in by_industry.values():
                column = {member.code: oriented[member.code] for member in members}
                zscores = zscore(
                    winsorize(
                        column,
                        lower_q=winsor_quantiles[0],
                        upper_q=winsor_quantiles[1],
                    )
                )
                for member in members:
                    member.zscores[factor_name] = zscores[member.code]
                    member.factor_metadata.setdefault(factor_name, {})[
                        "neutralization"
                    ] = diagnostics

    for members in by_industry.values():
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
                slope = (
                    sum(
                        (float(momentum) - momentum_mean) * (float(trend) - trend_mean)
                        for _member, momentum, trend in momentum_trend
                    )
                    / variance
                )
                for member, momentum, trend in momentum_trend:
                    member.zscores["trend"] = (
                        float(trend)
                        - trend_mean
                        - slope * (float(momentum) - momentum_mean)
                    )
        # 60/120日低波高度重叠：保留60日水平，120日仅贡献控制60日后的
        # 中长期增量，避免同一种波动风险在族内被重复计权。
        volatility_pair = [
            (
                member,
                member.zscores.get("volatility_60"),
                member.zscores.get("volatility_120"),
            )
            for member in members
            if member.zscores.get("volatility_60") is not None
            and member.zscores.get("volatility_120") is not None
        ]
        if len(volatility_pair) >= 3:
            short_mean = fmean(float(item[1]) for item in volatility_pair)
            long_mean = fmean(float(item[2]) for item in volatility_pair)
            variance = sum(
                (float(short) - short_mean) ** 2
                for _member, short, _long in volatility_pair
            )
            if variance > 0:
                slope = (
                    sum(
                        (float(short) - short_mean) * (float(long) - long_mean)
                        for _member, short, long in volatility_pair
                    )
                    / variance
                )
                for member, short, long in volatility_pair:
                    member.zscores["volatility_120"] = (
                        float(long) - long_mean - slope * (float(short) - short_mean)
                    )

    for result in results:
        family_structures: dict[str, object] = {}
        for family in DEFAULT_FAMILY_WEIGHTS:
            score, structure = _combine_family(result, family)
            setattr(result, family, score)
            family_structures[family] = structure
        result.model_structure["families"] = family_structures

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
                result.model_eligible
                and result.data_coverage + 1e-12 >= MIN_FAMILY_WEIGHT_COVERAGE
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
            numerator = sum((a - left_mean) * (b - right_mean) for a, b in pairs)
            denominator = math.sqrt(
                sum((a - left_mean) ** 2 for a, _b in pairs)
                * sum((b - right_mean) ** 2 for _a, b in pairs)
            )
            matrix[left][right] = numerator / denominator if denominator > 0 else None
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
