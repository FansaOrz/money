"""稳健组合 V2 风控纯函数测试（quant_risk）。

覆盖：市场层分类、基金家族识别与 A/C/D 去重、绝对动量 12-1、
层内前 30% 选取、EWMA60 波动、波动率目标系数（只降仓 + 10% 带宽）、
高波动+急反弹冻结、权重约束（单基金/家族/QDII）。
"""

import math

import pytest

from app.services import quant_risk as risk


# ---------------------------------------------------------------------------
# 市场层分类
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("易方达沪深300ETF联接A", "cn_300"),
        ("华夏恒生ETF联接A", "hk"),
        ("国泰纳斯达克100指数QDII", "us_nasdaq"),
        ("博时标普500ETF联接A", "us_spx"),
        ("华夏恒生科技ETF联接C", "hk_tech"),
        ("华安黄金易ETF联接A", "gold"),
        ("招商产业债券A", "bond"),
        ("天弘余额宝货币A", "money"),
        ("上投摩根全球新兴市场QDII", "overseas"),
        ("易方达消费行业股票", "cn"),
    ],
)
def test_classify_market(name: str, expected: str) -> None:
    assert risk.classify_market(name) == expected


def test_is_qdii() -> None:
    assert risk.is_qdii("us_nasdaq")
    assert risk.is_qdii("hk")
    assert risk.is_qdii("gold")
    assert not risk.is_qdii("cn")
    assert not risk.is_qdii("bond")
    assert not risk.is_qdii("money")


# ---------------------------------------------------------------------------
# 基金家族识别与份额去重
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "family"),
    [
        ("易方达沪深300ETF联接A", "沪深300ETF联接"),
        ("易方达沪深300ETF联接C", "沪深300ETF联接"),
        ("易方达沪深300ETF联接D", "沪深300ETF联接"),
        ("华夏恒生ETF联接（人民币）A", "恒生ETF联接（人民币）"),
    ],
)
def test_fund_family_share_classes(name: str, family: str) -> None:
    assert risk.fund_family(name) == family


def test_fund_family_strips_company_prefix() -> None:
    assert risk.fund_family("易方达消费行业股票") == "消费行业股票"
    # 同一家族不同公司前缀不应混淆（保留主体差异）
    assert risk.fund_family("华夏消费行业股票") == "消费行业股票"  # 同名不同公司按主体归并


def test_dedupe_share_classes_keeps_highest_momentum() -> None:
    candidates = [
        {"code": "110011A", "name": "易方达沪深300ETF联接A", "momentum": 0.05},
        {"code": "110011C", "name": "易方达沪深300ETF联接C", "momentum": 0.08},
        {"code": "110011D", "name": "易方达沪深300ETF联接D", "momentum": 0.03},
        {"code": "020001", "name": "华夏恒生ETF联接A", "momentum": 0.10},
    ]
    kept, dropped = risk.dedupe_share_classes(candidates)
    kept_codes = {item["code"] for item in kept}
    assert kept_codes == {"110011C", "020001"}
    assert sorted(dropped) == ["110011A", "110011D"]


def test_dedupe_share_classes_prefers_known_momentum() -> None:
    candidates = [
        {"code": "A", "name": "易方达XX债券A", "momentum": None},
        {"code": "C", "name": "易方达XX债券C", "momentum": 0.02},
    ]
    kept, dropped = risk.dedupe_share_classes(candidates)
    assert [item["code"] for item in kept] == ["C"]
    assert dropped == ["A"]


# ---------------------------------------------------------------------------
# 绝对动量 12-1
# ---------------------------------------------------------------------------


def _flat_then_rise(days: int, rise_from: int, daily: float = 0.001) -> list[float]:
    values = [1.0]
    for i in range(days - 1):
        values.append(values[-1] * (1 + (daily if i >= rise_from else 0.0)))
    return values


def test_absolute_momentum_skips_recent_21_days() -> None:
    """12-1 动量跳过最近 21 日：最后 21 日暴涨不影响动量值。"""
    base = _flat_then_rise(253, rise_from=0, daily=0.0005)
    boosted = base[:-21] + [v * 1.5 for v in base[-21:]]  # 最近 21 日 ×1.5
    m_base = risk.absolute_momentum_12_1(base)
    m_boosted = risk.absolute_momentum_12_1(boosted)
    assert m_base is not None and m_boosted is not None
    assert m_boosted == pytest.approx(m_base)  # 忽略最近 21 日


def test_absolute_momentum_formula() -> None:
    values = [1.0 + i * 0.001 for i in range(300)]
    m = risk.absolute_momentum_12_1(values)
    expected = values[-22] / values[-253] - 1.0
    assert m == pytest.approx(expected)


def test_absolute_momentum_insufficient_samples() -> None:
    assert risk.absolute_momentum_12_1([1.0] * 252) is None
    assert risk.absolute_momentum_12_1([1.0] * 253) == pytest.approx(0.0)


def test_absolute_momentum_non_positive_nav() -> None:
    values = [1.0] * 253
    values[-253] = 0.0
    assert risk.absolute_momentum_12_1(values) is None


# ---------------------------------------------------------------------------
# 层内前 30% 选取
# ---------------------------------------------------------------------------


def test_select_top_in_market_keeps_top_30pct() -> None:
    candidates = [{"code": f"F{i}", "momentum": 0.01 * i} for i in range(1, 11)]  # 10 只
    selected = risk.select_top_in_market(candidates)
    assert len(selected) == 3  # ceil(10 × 0.3)
    assert [item["code"] for item in selected] == ["F10", "F9", "F8"]
    assert [item["rank"] for item in selected] == [1, 2, 3]


def test_select_top_in_market_keeps_at_least_one() -> None:
    selected = risk.select_top_in_market([{"code": "F1", "momentum": 0.01}])
    assert len(selected) == 1


# ---------------------------------------------------------------------------
# EWMA60 波动
# ---------------------------------------------------------------------------


def test_ewma_volatility_flat_returns_near_zero() -> None:
    assert risk.ewma_volatility([0.0] * 60) == pytest.approx(0.0)


def test_ewma_volatility_annualizes() -> None:
    # 恒定 ±1% 交替日收益：方差 ≈ 1e-4，年化 ≈ sqrt(1e-4×252) ≈ 15.9%
    returns = [0.01 if i % 2 == 0 else -0.01 for i in range(60)]
    vol = risk.ewma_volatility(returns)
    assert vol == pytest.approx(math.sqrt(1e-4 * 252), rel=0.05)


def test_ewma_volatility_insufficient_samples() -> None:
    assert risk.ewma_volatility([0.01] * 10) is None


def test_ewma_weights_recent_more() -> None:
    """EWMA 对近期波动赋予更大权重：尾部放大的序列波动更高。"""
    calm = [0.001] * 60
    early_shock = [0.05] * 10 + calm[10:]
    late_shock = calm[:-10] + [0.05] * 10
    assert risk.ewma_volatility(late_shock) > risk.ewma_volatility(early_shock)


# ---------------------------------------------------------------------------
# 波动率目标系数（只降仓，10% 带宽）
# ---------------------------------------------------------------------------


def test_vol_scalar_within_band_is_one() -> None:
    assert risk.vol_target_scalar(0.105, target_vol=0.10) == 1.0  # ≤ 10%×1.1
    assert risk.vol_target_scalar(0.11, target_vol=0.10) == 1.0
    assert risk.vol_target_scalar(0.05, target_vol=0.10) == 1.0  # 只降仓：低波不升仓


def test_vol_scalar_reduces_above_band() -> None:
    # 实现波动 20% > 11%（目标+带宽）→ 系数 0.10/0.20 = 0.5
    assert risk.vol_target_scalar(0.20, target_vol=0.10) == pytest.approx(0.5)


def test_vol_scalar_never_exceeds_one() -> None:
    assert risk.vol_target_scalar(0.01, target_vol=0.10) <= 1.0
    assert risk.vol_target_scalar(None, target_vol=0.10) == 1.0  # 缺失不惩罚


# ---------------------------------------------------------------------------
# 高波动 + 急反弹冻结
# ---------------------------------------------------------------------------


def test_freeze_requires_both_high_vol_and_rebound() -> None:
    # 高波动但无急反弹 → 不冻结
    frozen, _ = risk.freeze_check([0.001] * 60, realized_vol=0.30)
    assert not frozen
    # 急反弹但波动不高 → 不冻结
    frozen, _ = risk.freeze_check([0.02] * 10, realized_vol=0.15)
    assert not frozen
    # 高波动 + 近5日 ≥8% → 冻结
    frozen, reason = risk.freeze_check([0.0] * 55 + [0.02] * 5, realized_vol=0.30)
    assert frozen and reason
    # 高波动 + 近10日 ≥12% → 冻结
    frozen, _ = risk.freeze_check([0.0] * 50 + [0.013] * 10, realized_vol=0.30)
    assert frozen


def test_freeze_missing_vol_no_freeze() -> None:
    frozen, _ = risk.freeze_check([0.02] * 10, realized_vol=None)
    assert not frozen


# ---------------------------------------------------------------------------
# 权重约束（单基金 8% / 家族 10% / QDII 30%）
# ---------------------------------------------------------------------------


def test_apply_weight_caps_single_fund_cap() -> None:
    weights = {"A": 0.5, "B": 0.5}
    families = {"A": "fa", "B": "fb"}
    markets = {"A": "cn", "B": "cn"}
    capped = risk.apply_weight_caps(weights, families, markets)
    assert capped["A"] <= 0.08 + 1e-9
    # 释放额度按比例再分配，B 同样被单基金 8% 截断 → 大量现金
    assert sum(capped.values()) <= 1.0


def test_apply_weight_caps_family_cap() -> None:
    # 同家族 3 只基金合计不得超过 10%
    weights = {"A1": 0.08, "A2": 0.08, "A3": 0.08, "B": 0.76}
    families = {"A1": "famA", "A2": "famA", "A3": "famA", "B": "famB"}
    markets = {code: "cn" for code in weights}
    capped = risk.apply_weight_caps(weights, families, markets)
    family_a = sum(w for code, w in capped.items() if families[code] == "famA")
    assert family_a <= 0.10 + 1e-9
    assert all(w <= 0.08 + 1e-9 for w in capped.values())


def test_apply_weight_caps_qdii_cap() -> None:
    weights = {"US1": 0.08, "US2": 0.08, "US3": 0.08, "US4": 0.08, "US5": 0.08, "CN1": 0.60}
    families = {code: code for code in weights}
    markets = {
        "US1": "us_nasdaq", "US2": "us_nasdaq", "US3": "us_spx",
        "US4": "us_spx", "US5": "hk", "CN1": "cn",
    }
    capped = risk.apply_weight_caps(weights, families, markets)
    qdii_total = sum(w for code, w in capped.items() if code.startswith("US"))
    assert qdii_total <= 0.30 + 1e-9


def test_apply_weight_caps_no_short_and_conservation() -> None:
    weights = {f"F{i}": 0.1 for i in range(10)}
    families = {code: code for code in weights}
    markets = {code: "cn" for code in weights}
    capped = risk.apply_weight_caps(weights, families, markets)
    assert all(w > 0 for w in capped.values())
    assert sum(capped.values()) <= 1.0 + 1e-9
    # 全部受 8% 截断：合计 = 10×0.08 = 0.8，其余为现金
    assert sum(capped.values()) == pytest.approx(0.8)


# ---------------------------------------------------------------------------
# 冻结：5/10 日收益按复利计算
# ---------------------------------------------------------------------------


def test_freeze_rebound_uses_compound_return() -> None:
    """近 10 日收益按复利（日收益连乘）而非简单求和。

    每日 +1.14%：简单求和 = 11.4% < 12%（不冻结）；
    复利 = 1.0114^10 - 1 ≈ 12.02% ≥ 12%（应冻结）。
    """
    daily = 0.0114
    assert sum([daily] * 10) < risk.FREEZE_REBOUND_10D  # 求和口径不触发
    assert (1 + daily) ** 10 - 1 >= risk.FREEZE_REBOUND_10D  # 复利口径触发
    frozen, reason = risk.freeze_check([0.0] * 50 + [daily] * 10, realized_vol=0.30)
    assert frozen and reason
    assert "急反弹" in reason or "反弹" in reason


def test_freeze_rebound_5d_compound() -> None:
    """近 5 日每日 +1.57%：求和 7.85% < 8%（不冻结），复利 ≈ 8.1% ≥ 8%（冻结）。"""
    daily = 0.0157
    assert sum([daily] * 5) < risk.FREEZE_REBOUND_5D
    assert (1 + daily) ** 5 - 1 >= risk.FREEZE_REBOUND_5D
    frozen, _ = risk.freeze_check([0.0] * 55 + [daily] * 5, realized_vol=0.30)
    assert frozen


# ---------------------------------------------------------------------------
# 市场分类：v1/v2 统一口径（QDII 一致）
# ---------------------------------------------------------------------------


def test_classify_market_consistent_with_v1_factors() -> None:
    """quant_factors.classify_market 委托 quant_risk：同一基金两侧归类一致。"""
    from app.services import quant_factors as factors

    for name in (
        "国泰纳斯达克100指数QDII",
        "上投摩根全球新兴市场QDII",
        "华夏恒生ETF联接A",
        "易方达沪深300ETF联接A",
        "招商产业债券A",
        "某qdii小写基金",
    ):
        assert factors.classify_market(name) == risk.classify_market(name)


def test_classify_market_qdii_keyword_lands_overseas() -> None:
    """名称仅含 QDII 字样（无国家/地区关键词）时统一归其他海外（overseas）。"""
    assert risk.classify_market("某某全球精选QDII") == "overseas"


# ---------------------------------------------------------------------------
# 权重约束：同分权重多轮再分配
# ---------------------------------------------------------------------------


def test_apply_weight_caps_tie_breaks_symmetrically() -> None:
    """同分集体触顶：组内同比例收敛，不会先到先得导致末位归零。"""
    markets = {code: "cn" for code in "ABCD"}
    tied = {code: 0.25 for code in "ABCD"}
    capped = risk.apply_weight_caps(
        tied, families=markets, markets=markets,
        max_fund=0.25, max_family=0.5, max_qdii=1.0,
    )
    # 市场（家族槽位）合计 ≤50%；同分四只同比例收敛 → 各 12.5%
    assert sum(capped.values()) == pytest.approx(0.5)
    assert len(set(capped.values())) == 1
    assert capped["A"] == pytest.approx(0.125)


def test_apply_weight_caps_redistributes_released_weight() -> None:
    """多轮再分配：高权重成员触顶后，其余成员按其请求占比继续分配剩余额度。"""
    # B 触 25% 顶固定，C/D 在下一轮按 0.15:0.05 = 3:1 占比分享剩余额度
    weights = {"A": 0.25, "B": 0.5, "C": 0.15, "D": 0.05}
    markets = {code: code for code in weights}  # 各自独立家族
    capped = risk.apply_weight_caps(
        weights, families=markets, markets=markets,
        max_fund=0.25, max_family=1.0, max_qdii=1.0,
    )
    assert capped["A"] <= 0.25 + 1e-9
    assert capped["B"] <= 0.25 + 1e-9
    # C/D 未触顶且按比例（3:1）分配，相对强弱保持
    assert capped["C"] > capped["D"]
    assert capped["C"] == pytest.approx(3 * capped["D"], rel=1e-3)
    assert sum(capped.values()) <= 1.0 + 1e-9


def test_apply_weight_caps_market_cap_never_exceeded() -> None:
    """家族/市场合计上限在任意多轮再分配后严格满足（回归：末轮不得绕过合计检查）。"""
    # 4 只同家族基金各自不超单基金顶，但合计远超家族顶
    weights = {f"F{i}": 0.2 for i in range(4)}
    families = {code: "same_family" for code in weights}
    markets = {code: "cn" for code in weights}
    capped = risk.apply_weight_caps(weights, families, markets)
    family_total = sum(capped.values())
    assert family_total <= risk.DEFAULT_MAX_FAMILY_WEIGHT + 1e-9
    # 同分同待遇
    assert len(set(capped.values())) == 1
