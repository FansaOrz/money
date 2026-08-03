"""规则模型纯函数因子库：市场分类、动量、风险调整动量、趋势、回撤、z-score、五档落档。

设计约束：
- 仅使用标准库（math/statistics），不依赖 pandas/numpy；
- 全部为纯函数，不访问数据库，便于单元测试；
- 仅使用评价日及之前的数据，所有窗口函数只取序列尾部。

因子公式（与已批准计划一致）：
- 动量 MOM = 0.5×R20 + 0.3×R60 + 0.2×R120，窗口不足时按可用窗口重新归一化权重；
- 风险调整动量 RAM60 = 近60日日收益均值 / 日收益标准差（为0时返回 None）；
- 趋势 TREND ∈ {-1, -0.5, 0, +0.5, +1}：现价与 MA20/MA60 的多空顺序；
- 回撤 DRAWDOWN = 近120日最大回撤（负数小数，如 -0.15）；
- 综合分 SCORE = 0.45×z(MOM) + 0.35×z(RAM60) + 0.20×TREND + 0.50×z(DRAWDOWN)；
- 五档：同市场分位数前10% → +2，70%~90% → +1，30%~70% → 0，10%~30% → -1，后10% → -2；
  落 ±2 需趋势配合（+2 要求趋势不弱，-2 要求趋势不强），否则回落到 ±1；
- 市场 Risk-off 时正信号降一档（+2→+1，+1→0），负向信号不加强。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date
from statistics import fmean

from app.services import quant_risk as _risk

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

TRADING_DAYS_PER_YEAR = 252
MIN_SAMPLES = 120  # 入选所需最少净值样本
MOMENTUM_WINDOWS: tuple[tuple[int, float], ...] = ((20, 0.5), (60, 0.3), (120, 0.2))
RAM_WINDOW = 60  # 风险调整动量窗口
DRAWDOWN_WINDOW = 120  # 回撤窗口
SCORE_WEIGHT_MOMENTUM = 0.45
SCORE_WEIGHT_RISK_ADJ = 0.35
SCORE_WEIGHT_TREND = 0.20
SCORE_WEIGHT_DRAWDOWN = 0.50

# 观察池市场（黄金/债券/货币/其他海外）：不参与股票基金横截面排名
# 与 quant_risk 保持同一划分（防御层 = 观察池），v1/v2 市场口径统一
OBSERVE_MARKETS = {"gold", "bond", "money", "overseas"}
EQUITY_MARKETS = {"cn", "cn_300", "hk", "hk_tech", "us_spx", "us_nasdaq"}

# 市场标签与关键词分类规则统一收敛到 quant_risk（v1/v2 同一口径）；
# 本模块保留引用以兼容既有调用方，不再维护独立副本。
MARKET_LABELS: dict[str, str] = _risk.MARKET_LABELS

# 市场 -> 跟踪指数代码（MarketIndex.code）；列表按优先级取第一个有行情的
MARKET_BENCHMARKS: dict[str, tuple[str, ...]] = {
    "us_nasdaq": ("IXIC",),
    "us_spx": ("SPX",),
    "hk_tech": ("HSTECH",),
    "hk": ("HSI",),
    "cn_300": ("CSI300",),
    "cn": ("SH000001",),
}


# ---------------------------------------------------------------------------
# 市场分类与标签
# ---------------------------------------------------------------------------


def classify_market(fund_name: str) -> str:
    """按基金名称关键词有序匹配市场；兜底为 A股 cn。

    v1/v2 统一口径：委托 quant_risk.classify_market（含 QDII 等扩展关键词），
    避免两套规则漂移造成同一基金在 v1 筛选与 v2 组合中落入不同市场层。
    """
    return _risk.classify_market(fund_name)


def market_label(market: str) -> str:
    """市场的中文标签。"""
    return MARKET_LABELS.get(market, market)


def is_equity_market(market: str) -> bool:
    """是否参与横截面排名的权益市场（黄金/债券/货币/其他海外为观察池）。"""
    return market in EQUITY_MARKETS


# ---------------------------------------------------------------------------
# 基础窗口函数
# ---------------------------------------------------------------------------


def window_slice(values: list[float], window: int) -> list[float]:
    """取序列尾部 window+1 个样本（计算 window 区间收益所需）。"""
    if len(values) < window + 1:
        return []
    return values[-window - 1 :]


def period_return(values: list[float], window: int) -> float | None:
    """近 window 个区间的收益率，需要 window+1 个样本。"""
    tail = window_slice(values, window)
    if not tail or tail[0] <= 0:
        return None
    return tail[-1] / tail[0] - 1.0


def daily_returns(values: list[float]) -> list[float]:
    """日收益序列。"""
    return [values[i] / values[i - 1] - 1.0 for i in range(1, len(values)) if values[i - 1] > 0]


def moving_average(values: list[float], window: int) -> float | None:
    """简单移动平均（最新值），样本不足返回 None。"""
    if len(values) < window or window <= 0:
        return None
    return fmean(values[-window:])


def max_drawdown(values: list[float], window: int | None = None) -> float | None:
    """最大回撤（负数小数）。window 指定时仅看最近 window+1 个样本。"""
    series = values if window is None else values[-window - 1 :]
    if len(series) < 2:
        return None
    peak = series[0]
    worst = 0.0
    for value in series:
        if value > peak:
            peak = value
        if peak > 0:
            drawdown = value / peak - 1.0
            if drawdown < worst:
                worst = drawdown
    return worst


# ---------------------------------------------------------------------------
# 因子
# ---------------------------------------------------------------------------


def momentum_score(values: list[float]) -> tuple[float | None, dict[str, float]]:
    """动量因子 MOM = 0.5×R20 + 0.3×R60 + 0.2×R120。

    窗口样本不足时剔除该项并按剩余权重重新归一化；全部不足返回 None。
    返回 (动量值, 各窗口收益 dict)。
    """
    returns: dict[str, float] = {}
    weighted = 0.0
    weight_sum = 0.0
    for window, weight in MOMENTUM_WINDOWS:
        r = period_return(values, window)
        if r is None:
            continue
        returns[f"r{window}"] = r
        weighted += weight * r
        weight_sum += weight
    if weight_sum <= 0:
        return None, returns
    return weighted / weight_sum, returns


def risk_adjusted_momentum(values: list[float], window: int = RAM_WINDOW) -> float | None:
    """风险调整动量：近 window 日日收益均值 / 日收益标准差。

    标准差为 0（恒定增长）时返回 None，由横截面缺失值处理兜底。
    """
    returns = daily_returns(values)[-window:]
    if len(returns) < 2:
        return None
    mean = fmean(returns)
    variance = sum((r - mean) ** 2 for r in returns) / (len(returns) - 1)
    std = math.sqrt(variance)
    if std == 0:
        return None
    return mean / std


def trend_strength(values: list[float]) -> tuple[float | None, dict[str, float | None]]:
    """趋势确认 ∈ {-1, -0.5, 0, +0.5, +1}，基于现价与 MA20/MA60 的多空顺序。

    评分：现价≥MA20、现价≥MA60、MA20≥MA60 各 +1/−1，归一化到 [-1, 1]。
    MA60 缺失时按两项归一化；MA20 也缺失返回 None。
    返回 (趋势值, 证据 dict)。
    """
    if not values:
        return None, {"price": None, "ma20": None, "ma60": None}
    price = values[-1]
    ma20 = moving_average(values, 20)
    ma60 = moving_average(values, 60)
    evidence: dict[str, float | None] = {"price": price, "ma20": ma20, "ma60": ma60}
    if ma20 is None:
        return None, evidence

    score = 0.0
    count = 0
    if ma20 is not None:
        score += 1.0 if price >= ma20 else -1.0
        count += 1
    if ma60 is not None:
        score += 1.0 if price >= ma60 else -1.0
        score += 1.0 if ma20 >= ma60 else -1.0
        count += 2
    if count == 0:
        return None, evidence
    return score / count, evidence


# ---------------------------------------------------------------------------
# 横截面统计
# ---------------------------------------------------------------------------


def zscores(values: dict[str, float | None]) -> dict[str, float | None]:
    """横截面 z-score：(x − μ) / σ（总体标准差）。

    样本 <2 或标准差为 0 时，有效值一律记 0（无横截面区分度）；
    None（缺失）保持 None，由调用方决定如何处理。
    """
    valid = [v for v in values.values() if v is not None]
    if len(valid) < 2:
        return {k: (0.0 if v is not None else None) for k, v in values.items()}
    mean = fmean(valid)
    variance = sum((v - mean) ** 2 for v in valid) / len(valid)
    std = math.sqrt(variance)
    if std == 0:
        return {k: (0.0 if v is not None else None) for k, v in values.items()}
    return {k: ((v - mean) / std if v is not None else None) for k, v in values.items()}


def quantile_ranks(values: dict[str, float | None]) -> dict[str, float | None]:
    """横截面分位数排名 ∈ [0, 1]：小于该值的样本占比。

    最高值的分位数为 (n−1)/n；并列取平均秩。None 保持 None。
    """
    valid = sorted(v for v in values.values() if v is not None)
    n = len(valid)
    if n == 0:
        return dict.fromkeys(values)
    result: dict[str, float | None] = {}
    for key, value in values.items():
        if value is None:
            result[key] = None
            continue
        below = sum(1 for v in valid if v < value)
        equal = sum(1 for v in valid if v == value)
        # 并列取平均秩：(below + below+equal-1) / 2 / n
        result[key] = (2 * below + equal - 1) / (2 * n)
    return result


def composite_score(
    momentum_z: float | None,
    risk_adj_z: float | None,
    trend: float | None,
    drawdown_z: float | None,
) -> float:
    """综合分：0.45×z(MOM) + 0.35×z(RAM60) + 0.20×TREND + 0.50×z(DRAWDOWN)。

    缺失项按 0 处理（样本 <2 时横截面 z-score 本就全为 0）。
    """
    return (
        SCORE_WEIGHT_MOMENTUM * (momentum_z or 0.0)
        + SCORE_WEIGHT_RISK_ADJ * (risk_adj_z or 0.0)
        + SCORE_WEIGHT_TREND * (trend or 0.0)
        + SCORE_WEIGHT_DRAWDOWN * (drawdown_z or 0.0)
    )


# ---------------------------------------------------------------------------
# 五档落档与市场状态
# ---------------------------------------------------------------------------

TIER_LABELS: dict[int, str] = {
    2: "值得研究加仓",
    1: "偏积极",
    0: "中性持有",
    -1: "偏谨慎",
    -2: "值得研究减仓",
}


def tier_from_quantile(quantile: float | None, trend: float | None) -> int:
    """按同市场分位数与趋势落五档：+2/+1/0/−1/−2。

    - ≥90% → +2（趋势不弱，即 trend > 0，否则回落 +1）
    - 70%～90% → +1
    - 30%～70% → 0
    - 10%～30% → −1
    - <10% → −2（趋势不强，即 trend < 0，否则回落 −1）

    分位数缺失（观察池等）返回 0。
    """
    if quantile is None:
        return 0
    if quantile >= 0.9:
        if trend is not None and trend > 0:
            return 2
        return 1
    if quantile >= 0.7:
        return 1
    if quantile < 0.1:
        if trend is not None and trend < 0:
            return -2
        return -1
    if quantile < 0.3:
        return -1
    return 0


def tier_label(tier: int) -> str:
    """五档的中文标签。"""
    return TIER_LABELS.get(tier, TIER_LABELS[0])


def adjust_tier_for_regime(tier: int, regime: str) -> int:
    """市场状态过滤：Risk-off 时正信号降一档，负向信号不加强。

    risk_off：+2→+1，+1→0；其余状态不调整。
    """
    if regime == "risk_off":
        if tier == 2:
            return 1
        if tier == 1:
            return 0
    return tier


def index_regime(
    values: list[float],
) -> tuple[str, dict[str, float | None]]:
    """指数市场状态：risk_on / neutral / risk_off。

    判定（样本不足 60 日返回 ("insufficient", ...)）：
    - risk_off：指数 < MA20 且 MA20 < MA60，或近20日动量 ≤ −5%；
    - risk_on：指数 ≥ MA20 且 MA20 ≥ MA60 且近20日动量 > 0；
    - 其余 neutral。
    """
    evidence: dict[str, float | None] = {
        "price": None,
        "ma20": None,
        "ma60": None,
        "momentum_20d": None,
    }
    if len(values) < 61:
        return "insufficient", evidence
    price = values[-1]
    ma20 = moving_average(values, 20)
    ma60 = moving_average(values, 60)
    momentum_20d = period_return(values, 20)
    evidence.update(
        {"price": price, "ma20": ma20, "ma60": ma60, "momentum_20d": momentum_20d}
    )
    if ma20 is None or ma60 is None or momentum_20d is None:
        return "insufficient", evidence
    if (price < ma20 and ma20 < ma60) or momentum_20d <= -0.05:
        return "risk_off", evidence
    if price >= ma20 and ma20 >= ma60 and momentum_20d > 0:
        return "risk_on", evidence
    return "neutral", evidence


# ---------------------------------------------------------------------------
# 候选因子容器（服务层组装用）
# ---------------------------------------------------------------------------


@dataclass
class FactorBundle:
    """一只候选基金的原始因子与横截面统计结果。"""

    code: str
    name: str
    market: str
    benchmark: str | None
    data_date: date
    sample_count: int
    momentum: float | None = None
    risk_adjusted: float | None = None
    trend: float | None = None
    drawdown: float | None = None
    score: float = 0.0
    quantile: float | None = None
    tier: int = 0
    regime_adjusted: bool = False
    target_weight: float = 0.0
    weight_capped: bool = False
    reasons: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def label(self) -> str:
        return tier_label(self.tier)

    def factors_dict(self) -> dict[str, float | None]:
        """响应用 factors 字段：原始因子值（未取 z）。"""
        return {
            "momentum": self.momentum,
            "risk_adjusted_momentum_60d": self.risk_adjusted,
            "trend": self.trend,
            "drawdown_120d": self.drawdown,
        }
