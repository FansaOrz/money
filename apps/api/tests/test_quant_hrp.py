"""层内 HRP 配置纯函数测试（quant_hrp）。

覆盖：相关矩阵、聚类排序、HRP 权重合法性与风险平价直觉、
回退链（HRP → 逆波动 → 等权）。
"""


import pytest

from app.services import quant_hrp as hrp


def _trend_panel(days: int, daily: float, noise: float = 0.0) -> list[float]:
    """确定性趋势序列（可选交替噪声）。"""
    values = [1.0]
    for i in range(days - 1):
        jitter = noise if i % 2 == 0 else -noise
        values.append(values[-1] * (1 + daily + jitter))
    return values


def _correlated_panels(days: int = 160) -> dict[str, list[float]]:
    """构造三块相关性不同的面板：A/B 高相关同低波，C 高波。"""
    a = _trend_panel(days, 0.0005, noise=0.002)
    b = [v * (1 + 0.0001 * (i % 3)) for i, v in enumerate(a)]  # 与 A 高度相关
    # C：与 A/B 反向且波动更大
    c = [1.0]
    for i in range(1, days):
        move = (a[i] / a[i - 1] - 1.0)
        c.append(c[-1] * (1 - 2.0 * move + (0.004 if i % 2 == 0 else -0.004)))
    return {"A": a, "B": b, "C": c}


# ---------------------------------------------------------------------------
# 基础统计
# ---------------------------------------------------------------------------


def test_aligned_return_matrix_requires_window() -> None:
    panels = {"A": _trend_panel(130, 0.001), "B": _trend_panel(130, 0.001)}
    aligned = hrp.aligned_return_matrix(panels, window=120)
    assert aligned is not None
    codes, matrix = aligned
    assert codes == ["A", "B"]
    assert all(len(row) == 120 for row in matrix)
    # 样本不足 → None
    assert hrp.aligned_return_matrix({"A": _trend_panel(100, 0.001)}, window=120) is None


def test_correlation_matrix_properties() -> None:
    panels = _correlated_panels()
    codes, matrix = hrp.aligned_return_matrix(panels, window=120)
    corr = hrp.correlation_matrix(matrix)
    n = len(codes)
    assert len(corr) == n and all(len(row) == n for row in corr)
    for i in range(n):
        assert corr[i][i] == pytest.approx(1.0)
        for j in range(n):
            assert -1.0 <= corr[i][j] <= 1.0
            assert corr[i][j] == pytest.approx(corr[j][i])
    ia, ib, ic = codes.index("A"), codes.index("B"), codes.index("C")
    assert corr[ia][ib] > 0.9  # A/B 高相关
    assert corr[ia][ic] < 0  # A/C 负相关


def test_correlation_matrix_constant_series_safe() -> None:
    panels = {"X": [1.0] * 130, "Y": _trend_panel(130, 0.001)}
    codes, matrix = hrp.aligned_return_matrix(panels, window=120)
    corr = hrp.correlation_matrix(matrix)
    ix, iy = codes.index("X"), codes.index("Y")
    assert corr[ix][iy] == 0.0  # 恒定序列与他人相关记 0


# ---------------------------------------------------------------------------
# 聚类与 HRP 权重
# ---------------------------------------------------------------------------


def test_cluster_order_groups_correlated() -> None:
    panels = _correlated_panels()
    codes, matrix = hrp.aligned_return_matrix(panels, window=120)
    corr = hrp.correlation_matrix(matrix)
    order = hrp.cluster_order(corr)
    assert sorted(order) == [0, 1, 2]
    # A/B 高相关应在排序中相邻
    ia, ib = codes.index("A"), codes.index("B")
    positions = {idx: pos for pos, idx in enumerate(order)}
    assert abs(positions[ia] - positions[ib]) == 1


def test_hrp_weights_valid_simplex() -> None:
    panels = _correlated_panels()
    codes, matrix = hrp.aligned_return_matrix(panels, window=120)
    corr = hrp.correlation_matrix(matrix)
    var = hrp.variances(matrix)
    weights = hrp.hrp_weights(corr, var)
    assert len(weights) == len(codes)
    assert all(w > 0 for w in weights)
    assert sum(weights) == pytest.approx(1.0)


def test_hrp_prefers_low_vol_asset() -> None:
    """HRP/逆方差直觉：高波动资产 C 的权重应显著低于低波高相关的 A/B 合计。"""
    panels = _correlated_panels()
    weights, method = hrp.allocate(panels, window=120)
    assert method == "hrp"
    assert weights["C"] < weights["A"] + weights["B"]
    assert weights["C"] < 0.5


def test_hrp_two_assets_inverse_variance() -> None:
    """两资产时 HRP 退化为逆方差分配。"""
    panels = {
        "LOW": _trend_panel(130, 0.0002, noise=0.0005),
        "HIGH": _trend_panel(130, 0.0002, noise=0.008),
    }
    weights, method = hrp.allocate(panels, window=120)
    assert method == "hrp"
    assert weights["LOW"] > weights["HIGH"]


# ---------------------------------------------------------------------------
# 回退链
# ---------------------------------------------------------------------------


def test_fallback_inverse_vol_when_insufficient_window() -> None:
    """样本不足 121 个净值点 → 回退逆波动（样本足够日收益）。"""
    panels = {
        "LOW": _trend_panel(80, 0.0002, noise=0.0005),
        "HIGH": _trend_panel(80, 0.0002, noise=0.008),
    }
    weights, method = hrp.allocate(panels, window=120)
    assert method == "inverse_vol"
    assert weights["LOW"] > weights["HIGH"]
    assert sum(weights.values()) == pytest.approx(1.0)


def test_fallback_equal_weight_when_constant() -> None:
    """全部净值恒定（日收益标准差为 0）→ 逆波动失效，回退等权。"""
    panels = {"A": [1.0] * 130, "B": [2.0] * 130}
    weights, method = hrp.allocate(panels, window=120)
    assert method == "equal_weight"
    assert weights == {"A": 0.5, "B": 0.5}


def test_allocate_single_asset() -> None:
    weights, method = hrp.allocate({"A": _trend_panel(130, 0.001)}, window=120)
    assert weights == {"A": 1.0}
    assert method == "equal_weight"


def test_allocate_empty() -> None:
    weights, method = hrp.allocate({}, window=120)
    assert weights == {}
    assert method == "equal_weight"
