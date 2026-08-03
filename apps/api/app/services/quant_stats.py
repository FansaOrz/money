"""量化验证统计指标（纯函数，便于单测与解析对照）。

全部为无状态纯函数，仅依赖标准库（math/statistics/random），不访问数据库：

- 风险指标：CVaR95（期望亏空 ES）、Calmar（年化收益 / |最大回撤|）、信息比率；
- 预测有效性：Rank IC（Spearman）、五档收益单调性（Kendall tau）；
- 多重检验稳健性：Deflated Sharpe Ratio 简化实现（Bailey & López de Prado 2014
  的期望最大夏普近似，记录 trial_count / skew / kurtosis）、
  block bootstrap 近似的 White Reality Check（White 2000）；
- 参数邻域稳定性：邻域样本外分位数与最优区间的稳定性带。

约定：
- 收益率为小数口径（日收益），年化按 252 个交易日折算；
- 偏度/峰度采用 Fisher 定义（与 López de Prado 的 DSR 推导一致）；
- bootstrap 使用显式 seed 的 random.Random，结果确定可复现。
"""

from __future__ import annotations

import math
import random
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from statistics import fmean

TRADING_DAYS_PER_YEAR = 252
DEFAULT_RISK_FREE_RATE = 0.02

# 标准正态分布函数的渐进截断点（|z| 超出后按 0/1 处理，避免数值问题）
_NORM_CDF_CLAMP = 8.0

# Euler–Mascheroni 常数（期望最大夏普近似用）
_EULER_GAMMA = 0.5772156649015329


# ---------------------------------------------------------------------------
# 基础统计
# ---------------------------------------------------------------------------


def _norm_cdf(z: float) -> float:
    """标准正态分布函数 Φ(z)。"""
    if z <= -_NORM_CDF_CLAMP:
        return 0.0
    if z >= _NORM_CDF_CLAMP:
        return 1.0
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def _sample_std(values: Sequence[float]) -> float | None:
    """样本标准差（n-1 口径）；样本不足 2 个返回 None。"""
    n = len(values)
    if n < 2:
        return None
    mean = fmean(values)
    variance = sum((v - mean) ** 2 for v in values) / (n - 1)
    return math.sqrt(variance)


def skewness(returns: Sequence[float]) -> float | None:
    """Fisher 偏度 γ3 = E[(x-μ)³] / σ³（矩估计，n 口径）。"""
    n = len(returns)
    if n < 2:
        return None
    mean = fmean(returns)
    m2 = sum((r - mean) ** 2 for r in returns) / n
    if m2 <= 0:
        return 0.0
    m3 = sum((r - mean) ** 3 for r in returns) / n
    return m3 / (m2 ** 1.5)


def kurtosis(returns: Sequence[float]) -> float | None:
    """Fisher 峰度 γ4 = E[(x-μ)⁴] / σ⁴ - 3（正态为 0，n 口径）。"""
    n = len(returns)
    if n < 2:
        return None
    mean = fmean(returns)
    m2 = sum((r - mean) ** 2 for r in returns) / n
    if m2 <= 0:
        return 0.0
    m4 = sum((r - mean) ** 4 for r in returns) / n
    return m4 / (m2 * m2) - 3.0


def sharpe_ratio(
    returns: Sequence[float], risk_free_rate: float = DEFAULT_RISK_FREE_RATE
) -> float | None:
    """年化夏普比率（日收益均值-日无风险利率）/日收益标准差×√252。"""
    if len(returns) < 2:
        return None
    daily_rf = risk_free_rate / TRADING_DAYS_PER_YEAR
    excess = [r - daily_rf for r in returns]
    std = _sample_std(excess)
    if std is None or std == 0:
        return None
    return fmean(excess) / std * math.sqrt(TRADING_DAYS_PER_YEAR)


def annualized_return(total_return: float, periods: int) -> float | None:
    """由区间总收益与交易日数折算年化收益（252 口径）。"""
    if periods < 0 or total_return <= -1.0:
        return None
    if periods == 0:
        return total_return
    return (1.0 + total_return) ** (TRADING_DAYS_PER_YEAR / periods) - 1.0


def max_drawdown(values: Sequence[float]) -> float | None:
    """最大回撤（负数小数），如 -0.15 表示从高点最多跌去 15%。"""
    if len(values) < 2:
        return None
    peak = values[0]
    worst = 0.0
    for value in values:
        if value > peak:
            peak = value
        if peak > 0:
            drawdown = value / peak - 1.0
            if drawdown < worst:
                worst = drawdown
    return worst


# ---------------------------------------------------------------------------
# 风险指标：CVaR95 / Calmar / 信息比率
# ---------------------------------------------------------------------------


def cvar95(returns: Sequence[float]) -> float | None:
    """CVaR95（95% 期望亏空 ES）：最差 5% 样本的收益均值（负数口径）。

    与历史模拟法 VaR95 一致的经验分位口径：排序后取尾部
    ceil(0.05×n) 个最小收益的算术平均；非参数、不假设分布。
    """
    if not returns:
        return None
    ordered = sorted(returns)
    tail = max(1, math.ceil(0.05 * len(ordered)))
    return fmean(ordered[:tail])


def calmar_ratio(
    total_return: float, periods: int, max_dd: float | None
) -> float | None:
    """Calmar 比率 = 年化收益 / |最大回撤|。

    回撤为 0（恒涨/不动序列）或样本不足时无意义，返回 None。
    """
    annual = annualized_return(total_return, periods)
    if annual is None or max_dd is None:
        return None
    if max_dd == 0:
        if annual > 0:
            return float("inf")
        return 0.0
    if max_dd > 0:
        return None
    return annual / abs(max_dd)


def information_ratio(
    strategy_returns: Sequence[float], benchmark_returns: Sequence[float]
) -> float | None:
    """信息比率 IR = 主动收益（策略-基准，逐日差）均值 / 跟踪误差 × √252。

    两序列按下标一一配对，长度不一致时按较短者截断；跟踪误差为 0
    （策略与基准逐日完全一致）时返回 None。
    """
    n = min(len(strategy_returns), len(benchmark_returns))
    if n < 2:
        return None
    active = [strategy_returns[i] - benchmark_returns[i] for i in range(n)]
    tracking_error = _sample_std(active)
    if tracking_error is None or tracking_error == 0:
        return None
    return fmean(active) / tracking_error * math.sqrt(TRADING_DAYS_PER_YEAR)


# ---------------------------------------------------------------------------
# Rank IC（Spearman）与五档收益单调性
# ---------------------------------------------------------------------------


def _ranks(values: Sequence[float]) -> list[float]:
    """秩次（1 起），并列取平均秩（average ranks for ties）。"""
    n = len(values)
    order = sorted(range(n), key=lambda i: values[i])
    ranks = [0.0] * n
    i = 0
    while i < n:
        j = i + 1
        while j < n and values[order[j]] == values[order[i]]:
            j += 1
        avg = (i + 1 + j) / 2.0  # 名次 i+1 .. j 的平均
        for k in range(i, j):
            ranks[order[k]] = avg
        i = j
    return ranks


def rank_ic(scores: Sequence[float], forward_returns: Sequence[float]) -> float | None:
    """Rank IC：因子得分与前瞻收益的 Spearman 秩相关系数（含并列修正）。

    ρ = Cov(rank_s, rank_r) / (σ_rank_s × σ_rank_r)；并列取平均秩后按
    Pearson 公式计算，等价于含结修正的 Spearman。样本 <3、或任一序列
    全部相同（方差为 0，相关无定义）时返回 None。
    """
    n = min(len(scores), len(forward_returns))
    if n < 3:
        return None
    rs = _ranks(scores[:n])
    rr = _ranks(forward_returns[:n])
    mean_s = fmean(rs)
    mean_r = fmean(rr)
    cov = sum((a - mean_s) * (b - mean_r) for a, b in zip(rs, rr, strict=True)) / n
    var_s = sum((a - mean_s) ** 2 for a in rs) / n
    var_r = sum((b - mean_r) ** 2 for b in rr) / n
    if var_s <= 0 or var_r <= 0:
        return None
    return cov / math.sqrt(var_s * var_r)


@dataclass(frozen=True)
class QuintileMonotonicity:
    """五档分组的前瞻收益单调性结果。

    quintile_returns 按分数从低到高排列（Q1 最低分组，Q5 最高分组）；
    spread 为 Q5-Q1；kendall_tau 为组序（1..5）与组均值的相关，
    无并列时 ∈ {-1, -2/3, -1/3, 0, 1/3, 2/3, 1}（n=5）；
    monotonic 为严格递增（每组均值都高于前一组）。
    """

    quintile_returns: tuple[float | None, ...]
    spread: float | None
    kendall_tau: float | None
    monotonic: bool


def _kendall_tau(x: Sequence[float], y: Sequence[float]) -> float | None:
    """Kendall tau-a（不修正并列）：2(C-D)/(n(n-1))。"""
    n = len(x)
    if n < 2:
        return None
    concordant = discordant = 0
    for i in range(n - 1):
        for j in range(i + 1, n):
            product = (x[j] - x[i]) * (y[j] - y[i])
            if product > 0:
                concordant += 1
            elif product < 0:
                discordant += 1
    return 2.0 * (concordant - discordant) / (n * (n - 1))


def quintile_monotonicity(
    scores: Sequence[float], forward_returns: Sequence[float]
) -> QuintileMonotonicity | None:
    """按分数升序分五档，检验前瞻收益是否单调递增。

    分组规则：按分数排序后按下标均分为 5 组（近似均衡，n 较小时
    部分组样本更少）；每组取前瞻收益均值。样本 <10（每组不足 2 个）
    返回 None。
    """
    n = min(len(scores), len(forward_returns))
    if n < 10:
        return None
    order = sorted(range(n), key=lambda i: scores[i])
    groups: list[list[int]] = [[] for _ in range(5)]
    for rank_pos, idx in enumerate(order):
        groups[min(rank_pos * 5 // n, 4)].append(idx)

    means: list[float | None] = []
    for group in groups:
        means.append(fmean(forward_returns[i] for i in group) if group else None)

    valid = [m for m in means if m is not None]
    spread = (valid[-1] - valid[0]) if len(valid) >= 2 else None
    tau = _kendall_tau([1.0, 2.0, 3.0, 4.0, 5.0], [m if m is not None else 0.0 for m in means])
    monotonic = all(
        means[i] is not None and means[i - 1] is not None and means[i] > means[i - 1]
        for i in range(1, 5)
    )
    return QuintileMonotonicity(
        quintile_returns=tuple(means),
        spread=spread,
        kendall_tau=tau,
        monotonic=monotonic,
    )


# ---------------------------------------------------------------------------
# Deflated Sharpe Ratio（简化稳健实现）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DeflatedSharpeResult:
    """Deflated Sharpe Ratio 结果（Bailey & López de Prado 2014 的简化近似）。

    - sr / sr_std：观测（年化）夏普与其标准误
      SR̂ 的方差 ≈ (1 - γ3·SR + (γ4-1)/4·SR²) / (T-1)，SR 为年化口径；
    - expected_max_sr：N 次独立试验下的期望最大夏普（有限样本近似
      E[max SR] ≈ sr_std × [(1-γ)Φ⁻¹(1-1/N) + γΦ⁻¹(1-1/(N·e))]）；
    - dsr：P(SR > E[max SR]) = Φ((SR - E[maxSR]) / sr_std)，
      即"观测夏普显著超出纯运气上界"的概率。
    """

    trial_count: int
    sample_count: int
    sharpe: float
    skew: float
    kurtosis: float
    sr_std: float
    expected_max_sr: float
    dsr: float


def _norm_ppf(p: float) -> float:
    """标准正态逆累积分布（Acklam 有理逼近，|误差| < 1.15e-9）。"""
    if p <= 0.0:
        return -_NORM_CDF_CLAMP
    if p >= 1.0:
        return _NORM_CDF_CLAMP

    # Acklam 系数
    a = (-3.969683028665376e01, 2.209460984245205e02, -2.759285104469687e02,
         1.383577518672690e02, -3.066479806614716e01, 2.506628277459239e00)
    b = (-5.447609879822406e01, 1.615858368580409e02, -1.556989798598866e02,
         6.680131188771972e01, -1.328068155288572e01)
    c = (-7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e00,
         -2.549732539343734e00, 4.374664141464968e00, 2.938163982698783e00)
    d = (7.784695709041462e-03, 3.224671290700398e-01,
         2.445134137142996e00, 3.754408661907416e00)

    p_low = 0.02425
    p_high = 1.0 - p_low
    if p < p_low:
        q = math.sqrt(-2.0 * math.log(p))
        return (((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / (
            (((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1.0
        )
    if p <= p_high:
        q = p - 0.5
        r = q * q
        return (((((a[0] * r + a[1]) * r + a[2]) * r + a[3]) * r + a[4]) * r + a[5]) * q / (
            ((((b[0] * r + b[1]) * r + b[2]) * r + b[3]) * r + b[4]) * r + 1.0
        )
    q = math.sqrt(-2.0 * math.log(1.0 - p))
    return -(((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / (
        (((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1.0
    )


def expected_max_sharpe(trial_count: int, sr_std: float) -> float:
    """N 次独立试验下期望最大夏普的有限样本近似（零真实技能假设）。

    E[max_N Z] ≈ (1-γ)Φ⁻¹(1-1/N) + γΦ⁻¹(1-1/(N·e))，γ 为 Euler 常数；
    乘以 SR 的标准误即得期望最大夏普。N ≤ 1 时为 0（无多重比较）。
    """
    if trial_count <= 1 or sr_std <= 0:
        return 0.0
    n = float(trial_count)
    z1 = _norm_ppf(1.0 - 1.0 / n)
    z2 = _norm_ppf(1.0 - 1.0 / (n * math.e))
    return sr_std * ((1.0 - _EULER_GAMMA) * z1 + _EULER_GAMMA * z2)


def deflated_sharpe(
    returns: Sequence[float],
    trial_count: int,
    risk_free_rate: float = DEFAULT_RISK_FREE_RATE,
) -> DeflatedSharpeResult | None:
    """Deflated Sharpe Ratio 简化实现。

    输入样本外日收益序列与总试验（参数组合）数；返回 None 当样本 <2
    或夏普标准误无法估计（零波动）。trial_count 至少为 1，传入 1 表示
    无多重比较（expected_max_sr = 0，退化为 P(SR>0)）。
    """
    sr = sharpe_ratio(returns, risk_free_rate)
    if sr is None:
        return None
    t = len(returns)
    skew = skewness(returns) or 0.0
    kurt = kurtosis(returns) or 0.0
    # Var(SR̂) ≈ (1 - γ3·SR + (γ4-1)/4·SR²) / (T-1)（SR 为年化口径）。
    # 该渐进近似在极端高夏普 + 负超额峰度时可能给出非正估计；此时退化
    # 为正态（γ3=0, γ4=0）的最小方差 1/(T-1)，保证 DSR 稳健可用。
    variance = (1.0 - skew * sr + (kurt - 1.0) / 4.0 * sr * sr) / max(t - 1, 1)
    if variance <= 0:
        variance = 1.0 / max(t - 1, 1)
    sr_std = math.sqrt(variance)
    trials = max(int(trial_count), 1)
    e_max = expected_max_sharpe(trials, sr_std)
    dsr = _norm_cdf((sr - e_max) / sr_std)
    return DeflatedSharpeResult(
        trial_count=trials,
        sample_count=t,
        sharpe=sr,
        skew=skew,
        kurtosis=kurt,
        sr_std=sr_std,
        expected_max_sr=e_max,
        dsr=dsr,
    )


# ---------------------------------------------------------------------------
# Block bootstrap White Reality Check（近似）
# ---------------------------------------------------------------------------


def circular_block_bootstrap(
    values: Sequence[float], size: int, block_length: int, rng: random.Random
) -> list[float]:
    """循环块自助法（circular block bootstrap）重抽样。

    将原序列首尾相连视为环，随机起点抽取长度 block_length 的连续块，
    拼接至 size 个样本；保留序列内部的时间相关性。block_length ≤ 1
    时退化为普通 iid bootstrap。
    """
    n = len(values)
    if n == 0 or size <= 0:
        return []
    block = max(1, min(block_length, n))
    result: list[float] = []
    while len(result) < size:
        start = rng.randrange(n)
        for offset in range(block):
            result.append(values[(start + offset) % n])
            if len(result) >= size:
                break
    return result


def mean_log_sharpe(returns: Sequence[float]) -> float | None:
    """log(1+r) 的均值 / 标准差（White Reality Check 的检验统计量口径）。

    White (2000) 使用平均收益；对数收益在几何意义上更稳健，
    且与累计净值的对数增长一致。零波动返回 None。
    """
    if len(returns) < 2:
        return None
    logs = [math.log(1.0 + r) for r in returns if r > -1.0]
    if len(logs) < 2:
        return None
    std = _sample_std(logs)
    if std is None:
        return None
    mean = fmean(logs)
    if std == 0:
        if mean > 0:
            return _NORM_CDF_CLAMP
        if mean < 0:
            return -_NORM_CDF_CLAMP
        return None
    return mean / std


@dataclass(frozen=True)
class RealityCheckResult:
    """White Reality Check 近似结果。

    p_value = (超过实际统计量的重抽样次数 + 1) / (resamples + 1)；
    null_mean 为零技能假设下重抽样统计量的均值（应接近 0）。
    """

    p_value: float
    observed_stat: float
    null_mean: float
    resamples: int
    block_length: int


def white_reality_check(
    strategy_returns: Sequence[float],
    benchmark_returns: Sequence[float],
    resamples: int = 500,
    block_length: int | None = None,
    seed: int = 42,
) -> RealityCheckResult | None:
    """Block bootstrap 近似的 White Reality Check。

    检验"策略相对基准的超额能力显著为正"：
    - 主动收益 a_t = r_strategy,t - r_benchmark,t（逐日差）；
    - 实际统计量 V = mean(log(1+a)) / std(log(1+a))；
    - 零假设（无超额能力）：主动收益去均值后重抽样，统计量分布以 0 为中心；
    - 重抽样采用循环块自助法保留时间相关性，块长缺省为 round(√T)。

    返回单边 p 值（重抽样统计量 ≥ 实际值的比例，含 +1 修正）。
    样本 <2、统计量无法估计时返回 None。结果对给定 seed 确定可复现。
    """
    n = min(len(strategy_returns), len(benchmark_returns))
    if n < 2:
        return None
    active = [strategy_returns[i] - benchmark_returns[i] for i in range(n)]
    observed = mean_log_sharpe(active)
    if observed is None:
        return None

    centered = [a - fmean(active) for a in active]
    block = block_length if block_length and block_length >= 1 else max(1, round(math.sqrt(n)))
    rng = random.Random(seed)
    exceedances = 0
    null_stats: list[float] = []
    for _ in range(max(resamples, 1)):
        sample = circular_block_bootstrap(centered, n, block, rng)
        stat = mean_log_sharpe(sample)
        if stat is None:
            continue
        null_stats.append(stat)
        if stat >= observed:
            exceedances += 1
    draws = len(null_stats)
    if draws == 0:
        return None
    return RealityCheckResult(
        p_value=(exceedances + 1) / (draws + 1),
        observed_stat=observed,
        null_mean=fmean(null_stats),
        resamples=draws,
        block_length=block,
    )


# ---------------------------------------------------------------------------
# 参数邻域稳定性
# ---------------------------------------------------------------------------


def empirical_quantile(values: Sequence[float], q: float) -> float | None:
    """经验分位数（线性插值，type 7 口径），q ∈ [0,1]。"""
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    q = min(max(q, 0.0), 1.0)
    pos = q * (len(ordered) - 1)
    lower = int(pos)
    upper = min(lower + 1, len(ordered) - 1)
    frac = pos - lower
    return ordered[lower] * (1.0 - frac) + ordered[upper] * frac


@dataclass(frozen=True)
class NeighborhoodStability:
    """参数邻域稳定性结果。

    - center_value：中心参数点的指标值；
    - neighborhood_quantile：中心点在邻域全部取值中的经验分位数 ∈[0,1]，
      越高表示中心参数不依赖"恰好选对参数"；
    - band_low / band_high：邻域取值去掉 min/max 后的范围
      （少于 3 个邻域点时退化为 min/max）；
    - neighbor_count：参与评估的邻域参数点数（含中心）。
    """

    center_value: float
    neighborhood_quantile: float
    band_low: float
    band_high: float
    neighbor_count: int


def neighborhood_stability(
    center_value: float, neighbor_values: Sequence[float]
) -> NeighborhoodStability | None:
    """中心参数在邻域（含中心）取值分布中的稳定性。

    分位数 = 邻域中 ≤ 中心值的比例（并列计一半），中心为邻域最优时
    接近 1；带 = 去掉一个最小与一个最大后的取值范围（样本 ≥3 时）。
    """
    values = [center_value, *[v for v in neighbor_values]]
    if not values:
        return None
    below = sum(1 for v in values if v < center_value)
    ties = sum(1 for v in values if v == center_value) - 1  # 排除中心自身
    quantile = (below + 0.5 * max(ties, 0)) / len(values)

    ordered = sorted(values)
    if len(ordered) >= 3:
        band = ordered[1:-1]
    else:
        band = ordered
    return NeighborhoodStability(
        center_value=center_value,
        neighborhood_quantile=quantile,
        band_low=band[0],
        band_high=band[-1],
        neighbor_count=len(values),
    )


# ---------------------------------------------------------------------------
# 权重扰动（供参数邻域稳定性复用的纯函数工具）
# ---------------------------------------------------------------------------


def perturb_weights(
    weights: Mapping[str, float], steps: Sequence[int] = (-1, 1)
) -> dict[int, dict[str, float]]:
    """对因子权重做单维 ±step 扰动，返回 {扰动后权重组的 key: 权重 dict}。

    key 为 (维度下标 << 8) | (step + 128) 的稳定编码；扰动结果与原值
    相同（权重为 0 的维度减 step）或扰动后为负（裁到 0）时仍保留，
    由调用方决定如何使用。
    """
    keys = sorted(weights)
    base = {k: float(weights[k]) for k in keys}
    variants: dict[int, dict[str, float]] = {}
    for dim_index, dim in enumerate(keys):
        for step in steps:
            variant = dict(base)
            variant[dim] = max(base[dim] + 0.05 * step, 0.0)
            variants[(dim_index << 8) | (step + 128)] = variant
    return variants
