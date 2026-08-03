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
    "gross_margin": 1,
    "ocf_to_profit": 1,
    "debt_ratio": -1,
}
VALUE_DIRECTIONS: dict[str, int] = {"ep": 1, "bp": 1, "valuation_percentile": 1}

MOMENTUM_WINDOW = 252  # 12 个月
MOMENTUM_SKIP = 21  # 跳过最近 1 个月（12-1）
TREND_MA_SHORT = 20
TREND_MA_LONG = 60
LOWVOL_WINDOW = 60
MIN_HISTORY_DAYS = 260  # 入选打分所需的最少历史 bar 数（覆盖 252+1 动量窗口）

WINSOR_LOWER_Q = 0.01
WINSOR_UPPER_Q = 0.99
WINSOR_MIN_SAMPLES = 8  # 行业内样本不足时不做 winsorize，直接 z-score
INDUSTRY_MIN_SAMPLES = 2  # 行业内 z-score 的最小样本

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


def _latest_fundamentals(
    fundamentals: tuple[Fundamentals, ...], as_of: date
) -> Fundamentals | None:
    """PIT：available_at ≤ as_of 的最新一条快照（输入已按日期升序）。"""
    result: Fundamentals | None = None
    for snapshot in fundamentals:
        if snapshot.available_at <= as_of:
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

    closes 为打分日 T 截止的收盘价升序序列；需要 ≥ 253 个点
    （252 区间 + 跳过 21 日后的两个端点），不足返回 None。
    """
    need = MOMENTUM_WINDOW + 1
    if len(closes) < need + MOMENTUM_SKIP:
        return None
    end_value = closes[-1 - MOMENTUM_SKIP]
    start_value = closes[-1 - MOMENTUM_SKIP - MOMENTUM_WINDOW]
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


def raw_factors(context: StockContext, as_of: date) -> dict[str, float | None]:
    """计算单只股票的全部原始因子（未经横截面处理）。

    无未来数据：价格序列已在 context 中按 ≤T 截断；基本面按 available_at
    ≤ T 取最新快照，估值分位的历史窗口同样按 T 过滤。
    """
    closes = [bar.close for bar in context.bars if not bar.suspended and bar.close > 0]
    latest = _latest_fundamentals(context.fundamentals, as_of)

    result: dict[str, float | None] = {
        "momentum_12_1": momentum_12_1(closes),
        "trend": trend_strength(closes),
        "lowvol": low_volatility(closes),
    }

    if latest is None:
        for name in QUALITY_DIRECTIONS:
            result[name] = None
        for name in VALUE_DIRECTIONS:
            result[name] = None
        return result

    result["roe"] = latest.roe
    result["gross_margin"] = latest.gross_margin
    result["ocf_to_profit"] = latest.ocf_to_profit
    result["debt_ratio"] = latest.debt_ratio
    result["ep"] = latest.ep
    result["bp"] = latest.bp

    current_values = [v for v in (latest.ep, latest.bp) if v is not None]
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
    "trend": "trend",
    "lowvol": "lowvol",
}
_FACTOR_DIRECTION: dict[str, int] = {
    **QUALITY_DIRECTIONS,
    **VALUE_DIRECTIONS,
    "momentum_12_1": 1,
    "trend": 1,
    "lowvol": 1,
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

    results: list[FactorResult] = []
    for context in contexts:
        raw = raw_factors(context, as_of)
        results.append(
            FactorResult(
                code=context.info.code,
                name=context.info.name,
                industry=context.info.industry or "未知",
                raw=raw,
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

    for result in results:
        result.quality = _combine_family(result.zscores, "quality")
        result.value = _combine_family(result.zscores, "value")
        result.momentum = _combine_family(result.zscores, "momentum")
        result.trend = _combine_family(result.zscores, "trend")
        result.lowvol = _combine_family(result.zscores, "lowvol")

        weighted = 0.0
        weight_sum = 0.0
        for family, weight in family_weights.items():
            family_score = getattr(result, family)
            if family_score is None:
                continue
            weighted += weight * family_score
            weight_sum += weight
        if weight_sum <= 0:
            result.composite = 0.0
            result.data_warnings.append("全部因子族缺失，复合分按 0 处理")
        else:
            result.composite = weighted / weight_sum
            if weight_sum < 1.0 - 1e-9:
                missing = [
                    family
                    for family in family_weights
                    if getattr(result, family) is None
                ]
                result.data_warnings.append(
                    f"因子族缺失（{'/'.join(missing)}），复合分按可用族权重归一化"
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


__all__ = [
    "DEFAULT_FAMILY_WEIGHTS",
    "FactorResult",
    "MAX_FACTOR_ROWS",
    "MIN_HISTORY_DAYS",
    "StockContext",
    "build_context",
    "compute_cross_section",
    "historical_percentile",
    "history_depth",
    "low_volatility",
    "momentum_12_1",
    "raw_factors",
    "trend_strength",
    "winsorize",
    "zscore",
]
