"""层内 HRP（层次风险平价）配置：纯函数实现，不依赖 pandas/numpy。

算法（López de Prado 简化版，递归二分）：
1. 输入：近 corr_window 个日收益的相关矩阵与各基金方差；
2. 距离 d(i,j) = sqrt((1 - corr(i,j)) / 2)，二次距离 D(i,j) = sqrt(mean_k(d(i,k)-d(j,k))²)；
3. 按平均连接（average linkage）凝聚聚类生成二叉树；
4. 从根节点递归二分：左右子树的簇方差用逆方差权重在簇内计算，
   资金按 alpha = 1 - var_left/(var_left+var_right) 分配，直至叶子。

任一环节数据不足或数值异常时，由调用方回退到逆波动 / 等权
（fallback_inverse_vol / fallback_equal_weight）。

全部为纯函数，便于单元测试。
"""

from __future__ import annotations

import math
from statistics import fmean

# 近 120 日相关窗口（日收益个数；需要 121 个净值点）
CORR_WINDOW = 120
MIN_HRP_ASSETS = 2


# ---------------------------------------------------------------------------
# 基础统计
# ---------------------------------------------------------------------------


def daily_returns(values: list[float]) -> list[float]:
    """日收益序列（与前值比较，前值非正则跳过）。"""
    return [values[i] / values[i - 1] - 1.0 for i in range(1, len(values)) if values[i - 1] > 0]


def aligned_return_matrix(
    panels: dict[str, list[float]], window: int = CORR_WINDOW
) -> tuple[list[str], list[list[float]]] | None:
    """取各基金尾部 window 个日收益，构造成 (codes, 矩阵[code][t])。

    要求每只基金都能提供恰好 window 个日收益（净值点 ≥ window+1）；
    任一基金样本不足时返回 None（由调用方回退）。
    """
    codes = sorted(panels)
    matrix: list[list[float]] = []
    for code in codes:
        values = panels[code]
        if len(values) < window + 1:
            return None
        returns = [
            values[i] / values[i - 1] - 1.0
            for i in range(len(values) - window, len(values))
            if values[i - 1] > 0
        ]
        if len(returns) != window:
            return None
        matrix.append(returns)
    return codes, matrix


def correlation_matrix(matrix: list[list[float]]) -> list[list[float]]:
    """由收益矩阵计算皮尔逊相关矩阵（对称、对角为 1）。

    某资产收益方差为 0（恒定序列）时，其与他人的相关记 0。
    """
    n = len(matrix)
    corr = [[0.0] * n for _ in range(n)]
    for i in range(n):
        corr[i][i] = 1.0
    for i in range(n):
        xi = matrix[i]
        mi = fmean(xi)
        devi = [x - mi for x in xi]
        vari = sum(d * d for d in devi)
        for j in range(i + 1, n):
            xj = matrix[j]
            mj = fmean(xj)
            devj = [x - mj for x in xj]
            varj = sum(d * d for d in devj)
            if vari <= 0 or varj <= 0:
                corr[i][j] = corr[j][i] = 0.0
                continue
            cov = sum(a * b for a, b in zip(devi, devj, strict=True))
            value = cov / math.sqrt(vari * varj)
            # 数值裁剪到 [-1, 1]
            corr[i][j] = corr[j][i] = max(-1.0, min(1.0, value))
    return corr


def variances(matrix: list[list[float]]) -> list[float]:
    """各资产的收益方差（总体口径）。"""
    result: list[float] = []
    for series in matrix:
        mean = fmean(series)
        result.append(sum((x - mean) ** 2 for x in series) / len(series))
    return result


# ---------------------------------------------------------------------------
# 凝聚聚类（平均连接），输出二叉树叶子顺序
# ---------------------------------------------------------------------------


def _distance_matrix(corr: list[list[float]]) -> list[list[float]]:
    """相关 -> 距离 -> 二次距离（López de Prado 式）。"""
    n = len(corr)
    d = [[math.sqrt(max((1.0 - corr[i][j]) / 2.0, 0.0)) for j in range(n)] for i in range(n)]
    dist = [[0.0] * n for _ in range(n)]
    for i in range(n):
        for j in range(i + 1, n):
            value = math.sqrt(
                sum((d[i][k] - d[j][k]) ** 2 for k in range(n)) / n
            )
            dist[i][j] = dist[j][i] = value
    return dist


def cluster_order(corr: list[list[float]]) -> list[int]:
    """平均连接凝聚聚类，返回叶子的排列顺序（同一簇的叶子相邻）。"""
    n = len(corr)
    if n == 1:
        return [0]
    dist = _distance_matrix(corr)
    # 每个簇存 (成员集合)；簇间距离 = 平均两两距离
    clusters: list[list[int]] = [[i] for i in range(n)]
    while len(clusters) > 1:
        best: tuple[float, int, int] | None = None
        for a in range(len(clusters)):
            for b in range(a + 1, len(clusters)):
                total = 0.0
                count = 0
                for i in clusters[a]:
                    for j in clusters[b]:
                        total += dist[i][j]
                        count += 1
                avg = total / count if count else 0.0
                if best is None or avg < best[0]:
                    best = (avg, a, b)
        assert best is not None
        _, a, b = best
        merged = clusters[a] + clusters[b]
        clusters = [c for k, c in enumerate(clusters) if k not in (a, b)]
        clusters.append(merged)
    return clusters[0]


# ---------------------------------------------------------------------------
# 递归二分（准对角化 + 簇间逆方差分配）
# ---------------------------------------------------------------------------


def _cluster_variance(corr: list[list[float]], var: list[float], items: list[int]) -> float:
    """簇方差：簇内资产按逆方差加权后的组合方差。"""
    if len(items) == 1:
        return max(var[items[0]], 0.0)
    inv = [1.0 / v if v > 0 else 0.0 for v in (var[i] for i in items)]
    total = sum(inv)
    if total <= 0:
        weights = [1.0 / len(items)] * len(items)
    else:
        weights = [x / total for x in inv]
    value = 0.0
    for a, ia in enumerate(items):
        for b, ib in enumerate(items):
            value += weights[a] * weights[b] * corr[ia][ib] * math.sqrt(
                max(var[ia], 0.0) * max(var[ib], 0.0)
            )
    return max(value, 0.0)


def hrp_weights(corr: list[list[float]], var: list[float]) -> list[float]:
    """HRP 权重：递归二分分配，返回与输入顺序一致的权重（合计为 1）。"""
    n = len(corr)
    if n == 0:
        return []
    if n == 1:
        return [1.0]
    order = cluster_order(corr)
    weights = [1.0] * n
    # 递归二分栈：每个元素是有序的索引片段
    stack: list[list[int]] = [order]
    while stack:
        items = stack.pop()
        if len(items) <= 1:
            continue
        split = len(items) // 2
        left, right = items[:split], items[split:]
        var_left = _cluster_variance(corr, var, left)
        var_right = _cluster_variance(corr, var, right)
        denom = var_left + var_right
        alpha_left = 1.0 - var_left / denom if denom > 0 else 0.5
        alpha_left = max(0.0, min(1.0, alpha_left))
        for i in left:
            weights[i] *= alpha_left
        for i in right:
            weights[i] *= 1.0 - alpha_left
        stack.append(left)
        stack.append(right)
    total = sum(weights)
    if total <= 0:
        return [1.0 / n] * n
    return [w / total for w in weights]


def allocate(
    panels: dict[str, list[float]], window: int = CORR_WINDOW
) -> tuple[dict[str, float], str]:
    """层内配置入口：优先 HRP，失败回退逆波动，再失败回退等权。

    返回 ({code: 权重（合计 1）}, 方法标识 hrp / inverse_vol / equal_weight)。
    """
    codes = sorted(panels)
    if not codes:
        return {}, "equal_weight"
    if len(codes) == 1:
        return {codes[0]: 1.0}, "equal_weight"

    aligned = aligned_return_matrix(panels, window)
    if aligned is not None:
        ordered_codes, matrix = aligned
        try:
            corr = correlation_matrix(matrix)
            var = variances(matrix)
            # 退化输入（全部零方差：恒定净值）时 HRP 无区分度，回退
            if sum(var) > 0:
                raw = hrp_weights(corr, var)
                weights = {code: raw[i] for i, code in enumerate(ordered_codes)}
                if all(w >= 0 for w in weights.values()) and sum(weights.values()) > 0:
                    return weights, "hrp"
        except (ValueError, ZeroDivisionError, OverflowError):
            pass  # 数值异常时回退

    # 回退 1：逆波动（窗口内日收益标准差倒数）
    inv_vol: dict[str, float] = {}
    for code in codes:
        values = panels[code]
        returns = daily_returns(values)[-window:]
        if len(returns) < 2:
            inv_vol = {}
            break
        mean = fmean(returns)
        variance = sum((r - mean) ** 2 for r in returns) / (len(returns) - 1)
        std = math.sqrt(variance)
        inv_vol[code] = 1.0 / std if std > 0 else 0.0
    if inv_vol and sum(inv_vol.values()) > 0:
        total = sum(inv_vol.values())
        return {code: value / total for code, value in inv_vol.items()}, "inverse_vol"

    # 回退 2：等权
    return {code: 1.0 / len(codes) for code in codes}, "equal_weight"
