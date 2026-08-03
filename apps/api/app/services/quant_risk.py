"""稳健组合 V2 风控纯函数库。

包含：
- 市场层分类（v1/v2 统一口径：quant_factors.classify_market 委托本模块，
  避免两套关键词漂移，如 QDII 字样基金在两侧归类不一致）；
- 同基金家族识别与 A/C/D 份额去重；
- 绝对动量 12-1（t-21 收盘 / t-252 前一日收盘 - 1，跳过最近 21 个交易日）；
- EWMA60 组合年化波动（lambda=0.94，RiskMetrics 口径）；
- 波动率目标仓位系数（只降仓，10% 带宽防抖）；
- 高波动 + 急反弹冻结判定。

全部为纯函数，不访问数据库，便于单元测试。
"""

from __future__ import annotations

import math
import re

TRADING_DAYS_PER_YEAR = 252

# ---- 绝对动量 12-1 ----
MOMENTUM_LOOKBACK = 252  # 动量长窗口（交易日）
MOMENTUM_SKIP = 21  # 跳过最近一个月（交易日）
MIN_MOMENTUM_SAMPLES = MOMENTUM_LOOKBACK + 1  # 需要 253 个净值点

# ---- EWMA 波动 ----
EWMA_LAMBDA = 0.94
EWMA_WINDOW = 60  # 估计窗口（日收益个数）
MIN_EWMA_SAMPLES = 20  # 不足该样本数时返回 None

# ---- 波动率目标 ----
DEFAULT_TARGET_VOL = 0.10  # 组合年化波动目标
VOL_BAND = 0.10  # 10% 带宽：实现波动在目标 ±10% 以内时系数按 1 处理（防抖）

# ---- 冻结规则（高波动 + 急反弹）----
FREEZE_HIGH_VOL = 0.25  # 高波阈值：EWMA60 年化波动 ≥ 25%
FREEZE_REBOUND_5D = 0.08  # 急反弹阈值：近 5 日组合收益 ≥ 8%
FREEZE_REBOUND_10D = 0.12  # 或近 10 日 ≥ 12%

# ---- 权重约束 ----
DEFAULT_MAX_FUND_WEIGHT = 0.08  # 单基金 8%
DEFAULT_MAX_FAMILY_WEIGHT = 0.10  # 同基金家族 10%
DEFAULT_MAX_QDII_WEIGHT = 0.30  # QDII（海外层）合计 30%

# 参与层内 HRP 的权益层；黄金/债券/货币/海外为防御层
EQUITY_MARKETS = {"cn", "cn_300", "hk", "hk_tech", "us_spx", "us_nasdaq"}
DEFENSIVE_MARKETS = {"gold", "bond", "money", "overseas"}
# QDII 口径：可投资海外的层（受 30% 合计上限约束）
QDII_MARKETS = {"us_spx", "us_nasdaq", "hk", "hk_tech", "overseas", "gold"}

MARKET_LABELS: dict[str, str] = {
    "us_nasdaq": "美股·纳斯达克",
    "us_spx": "美股·标普",
    "hk_tech": "港股·恒生科技",
    "hk": "港股",
    "cn_300": "A股·沪深300",
    "cn": "A股",
    "gold": "黄金",
    "bond": "债券",
    "money": "货币",
    "overseas": "其他海外",
}

# 基金名称关键词 -> 市场层，按顺序匹配（先细后粗）
_MARKET_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("us_nasdaq", ("纳斯达克", "纳指")),
    ("us_spx", ("标普", "道琼斯", "美国REIT", "美国 REIT")),
    ("hk_tech", ("恒生科技",)),
    ("hk", ("港股", "恒生", "沪港深", "香港", "H股", "中概", "大中华")),
    ("cn_300", ("沪深300", "沪深 300")),
    ("gold", ("黄金",)),
    ("bond", ("债券", "债基", "纯债", "信用债", "利率债", "可转债")),
    ("money", ("货币",)),
    ("overseas", ("美国", "美股", "海外", "全球", "美元", "德国", "日本", "印度", "越南", "法国", "英国", "QDII", "qdii")),
)


# ---------------------------------------------------------------------------
# 市场层分类
# ---------------------------------------------------------------------------


def classify_market(fund_name: str) -> str:
    """按基金名称关键词有序匹配市场层；兜底为 A股 cn。"""
    for market, keywords in _MARKET_RULES:
        if any(keyword in fund_name for keyword in keywords):
            return market
    return "cn"


def market_label(market: str) -> str:
    return MARKET_LABELS.get(market, market)


def is_qdii(market: str) -> bool:
    """是否按 QDII 口径管理（海外层，受合计 30% 上限约束）。"""
    return market in QDII_MARKETS


# ---------------------------------------------------------------------------
# 基金家族识别与份额去重
# ---------------------------------------------------------------------------

# 份额后缀：结尾的 A/C/D/E/B/I/H/R 类份额标记（含括号与“（前端）”等描述）
_SHARE_SUFFIX = re.compile(
    r"[\s(（]*(?:[A-ZＡ-Ｚ]{1,2}类?|A类|C类|D类|A份额|C份额|D份额|\(?前端\)?|\(?后端\)?)[)）]*\s*$"
)
# 常见公司前缀（用于从全称中提取“家族”主体；列表外的名称回退为前缀截取）
_COMPANY_PREFIXES = (
    "易方达", "华夏", "嘉实", "南方", "博时", "广发", "汇添富", "富国", "招商",
    "工银瑞信", "工银", "建信", "中银", "银华", "鹏华", "国泰", "华安", "大成",
    "景顺长城", "兴全", "兴证全球", "中欧", "交银施罗德", "交银", "上投摩根",
    "摩根", "华宝", "华泰柏瑞", "天弘", "诺安", "银河", "融通", "长信", "国投瑞银",
    "万家", "申万菱信", "农银汇理", "汇丰晋信", "宝盈", "泰达宏利", "宏利",
    "国联安", "海富通", "信诚", "中信保诚", "光大保德", "东方红", "东证资管",
    "平安", "民生加银", "浦银安盛", "永赢", "鑫元", "创金合信", "前海开源",
    "金鹰", "泰信", "中海", "东吴", "国海富兰克林", "富兰克林", "华富", "长盛",
    "新华", "安信", "圆信永丰", "红土创新", "九泰", "泓德", "嘉合", "恒越",
    "睿远", "泉果", "贝莱德", "富达", "路博迈", "施罗德", "联博", "宏利",
    "鹏扬", "淳厚", "蜂巢", "达诚", "尚正", "兴华", "明亚", "华西", "东海",
    "中金", "东兴", "红塔红土", "华宸未来", "江信", "新沃", "德邦", "英大",
    "太平", "国融", "合煦智远", "惠升", "同泰", "博远", "格林", "百嘉",
)


def fund_family(fund_name: str) -> str:
    """提取基金家族标识：去掉份额后缀与公司前缀差异，同一只基金的 A/C/D 份额归一家族。

    规则：先剥离结尾的份额标记（A/C/D 类/份额、前后端），再剥离已知的
    公司前缀；剩余主体（至少保留 2 个字符）作为家族键；过短时回退原名。
    """
    name = fund_name.strip()
    base = _SHARE_SUFFIX.sub("", name).strip()
    for prefix in _COMPANY_PREFIXES:
        if base.startswith(prefix) and len(base) > len(prefix) + 1:
            return base[len(prefix):]
    return base if len(base) >= 2 else name


def dedupe_share_classes(
    candidates: list[dict], momentum_key: str = "momentum"
) -> tuple[list[dict], list[str]]:
    """同基金家族 A/C/D 份额去重：每家族保留动量最高的一只（缺失动量视为最弱）。

    candidates 元素需含 code / name，以及 momentum_key 指向的动量值（可为 None）。
    返回 (去重后列表, 被剔除的代码列表)。
    """
    best_by_family: dict[str, dict] = {}
    dropped: list[str] = []
    order: list[str] = []
    for item in candidates:
        family = fund_family(item["name"])
        current = best_by_family.get(family)
        if current is None:
            best_by_family[family] = item
            order.append(family)
            continue
        current_mom = current.get(momentum_key)
        new_mom = item.get(momentum_key)
        # 动量缺失视为 -inf，保证有动量的份额优先
        current_score = current_mom if current_mom is not None else -math.inf
        new_score = new_mom if new_mom is not None else -math.inf
        if new_score > current_score:
            dropped.append(current["code"])
            best_by_family[family] = item
        else:
            dropped.append(item["code"])
    return [best_by_family[family] for family in order], dropped


# ---------------------------------------------------------------------------
# 绝对动量 12-1
# ---------------------------------------------------------------------------


def absolute_momentum_12_1(
    values: list[float],
    lookback: int = MOMENTUM_LOOKBACK,
    skip: int = MOMENTUM_SKIP,
) -> float | None:
    """绝对动量 12-1：values[-skip-1] / values[-lookback-1] - 1。

    即 t-21 日收盘相对 t-252 日前一日收盘的区间收益（跳过最近 21 个交易日，
    规避短期反转）。需要 lookback+1 个净值点；样本不足或净值为非正返回 None。
    """
    if len(values) < lookback + 1:
        return None
    recent = values[-skip - 1]
    base = values[-lookback - 1]
    if base <= 0 or recent <= 0:
        return None
    return recent / base - 1.0


def select_top_in_market(
    candidates: list[dict], top_ratio: float = 0.30, momentum_key: str = "momentum"
) -> list[dict]:
    """同类市场层内按动量取前 top_ratio（向上取整，至少 1 只）。

    candidates 需含 momentum_key 字段且已通过绝对动量 > 0 过滤；
    返回按动量降序的入选子集（附加 rank 字段，1 为最强）。
    """
    ranked = sorted(
        candidates,
        key=lambda item: (item.get(momentum_key) is not None, item.get(momentum_key) or 0.0),
        reverse=True,
    )
    keep = max(1, math.ceil(len(ranked) * top_ratio)) if ranked else 0
    selected = ranked[:keep]
    for rank, item in enumerate(selected, start=1):
        item["rank"] = rank
    return selected


# ---------------------------------------------------------------------------
# EWMA60 波动与波动率目标
# ---------------------------------------------------------------------------


def ewma_volatility(
    returns: list[float],
    lam: float = EWMA_LAMBDA,
    window: int = EWMA_WINDOW,
) -> float | None:
    """EWMA 年化波动（RiskMetrics 口径）：σ²_t = λ·σ²_{t-1} + (1-λ)·r²_{t-1}。

    仅使用尾部 window 个日收益（不足 window 时有多少用多少，至少
    MIN_EWMA_SAMPLES 个）；以窗口内简单方差初始化，向尾部递推。
    返回年化波动（小数）；样本不足返回 None。
    """
    tail = returns[-window:]
    if len(tail) < MIN_EWMA_SAMPLES:
        return None
    mean = sum(tail) / len(tail)
    var = sum((r - mean) ** 2 for r in tail) / len(tail)
    for r in tail:
        var = lam * var + (1 - lam) * r * r
    return math.sqrt(var * TRADING_DAYS_PER_YEAR)


def vol_target_scalar(
    realized_vol: float | None,
    target_vol: float = DEFAULT_TARGET_VOL,
    band: float = VOL_BAND,
) -> float:
    """波动率目标仓位系数（只降仓）：target/realized，封顶 1。

    10% 带宽防抖：realized_vol ≤ target×(1+band) 时按 1 处理；
    realized_vol 缺失（样本不足）时按 1 处理（不惩罚数据缺口）。
    """
    if realized_vol is None or realized_vol <= 0:
        return 1.0
    if realized_vol <= target_vol * (1 + band):
        return 1.0
    return min(1.0, target_vol / realized_vol)


# ---------------------------------------------------------------------------
# 高波动 + 急反弹冻结
# ---------------------------------------------------------------------------


def _compounded_return(returns: list[float]) -> float:
    """区间收益（复利）：日收益连乘 - 1，而非简单求和（低估反弹幅度）。"""
    wealth = 1.0
    for r in returns:
        wealth *= 1.0 + r
    return wealth - 1.0


def freeze_check(
    portfolio_returns: list[float],
    realized_vol: float | None,
    high_vol: float = FREEZE_HIGH_VOL,
    rebound_5d: float = FREEZE_REBOUND_5D,
    rebound_10d: float = FREEZE_REBOUND_10D,
) -> tuple[bool, str | None]:
    """冻结判定：EWMA60 年化波动 ≥ high_vol 且（近5日复利收益 ≥ 8% 或近10日复利 ≥ 12%）。

    返回 (是否冻结, 原因说明)。波动缺失时不冻结；5/10 日收益按复利
    （日收益连乘）计算，与净值曲线口径一致。
    """
    if realized_vol is None or realized_vol < high_vol:
        return False, None
    ret_5d = _compounded_return(portfolio_returns[-5:]) if len(portfolio_returns) >= 5 else 0.0
    ret_10d = _compounded_return(portfolio_returns[-10:]) if len(portfolio_returns) >= 10 else 0.0
    if ret_5d >= rebound_5d or ret_10d >= rebound_10d:
        reason = (
            f"组合 EWMA60 年化波动 {realized_vol:.1%} ≥ {high_vol:.0%}（高波动）"
            f"且近5日收益 {ret_5d:.1%} / 近10日收益 {ret_10d:.1%} 触发急反弹阈值，"
            "本期冻结调仓、沿用上一期持仓"
        )
        return True, reason
    return False, None


# ---------------------------------------------------------------------------
# 权重约束（单基金 / 家族 / QDII 上限，瀑布式再分配）
# ---------------------------------------------------------------------------


def apply_weight_caps(
    weights: dict[str, float],
    families: dict[str, str],
    markets: dict[str, str],
    max_fund: float = DEFAULT_MAX_FUND_WEIGHT,
    max_family: float = DEFAULT_MAX_FAMILY_WEIGHT,
    max_qdii: float = DEFAULT_MAX_QDII_WEIGHT,
) -> dict[str, float]:
    """对目标权重施加 单基金 ≤8% / 家族合计 ≤10% / QDII 合计 ≤30% 约束。

    多轮再分配（water-filling）：每轮在未封顶成员上找出超限最严重的约束组
    （单基金、家族合计或 QDII 合计，按 超出量/上限 的相对口径比较），将该组
    整体按组内请求占比收敛到其剩余额度并固定，释放的额度在下一轮按未封顶
    成员的请求占比再分配，直至全部约束满足。组内同分成员同比例收敛（不会
    先到先得导致末位归零）；每轮至少固定一个成员、保证收敛。无法再分配的
    截断部分返回为现金（权重合计 ≤ 1，不卖空）。
    """
    if not weights:
        return {}
    remaining = {code: w for code, w in weights.items() if w > 0}
    fixed: dict[str, float] = {}

    def _relative_excess(total: float, cap: float) -> float:
        """相对超限程度（>0 表示违例）；cap≤0 且仍有请求时视为无限违例。"""
        if cap <= 0:
            return math.inf if total > 0 else 0.0
        return max(0.0, (total - cap) / cap)

    for _ in range(2 * len(weights) + 2):
        if not remaining:
            break
        # 候选约束组：单基金 / 家族合计 / QDII 合计（仅统计未封顶成员）
        groups: list[tuple[float, dict[str, float], float]] = []  # (相对超限, 组, 额度)
        for code, w in remaining.items():
            groups.append((_relative_excess(w, max_fund), {code: w}, max_fund))
        family_groups: dict[str, dict[str, float]] = {}
        for code, w in remaining.items():
            family_groups.setdefault(families.get(code, code), {})[code] = w
        for family, members in family_groups.items():
            fixed_in_family = sum(
                v for c, v in fixed.items() if families.get(c, c) == family
            )
            room = max(max_family - fixed_in_family, 0.0)
            groups.append((_relative_excess(sum(members.values()), room), members, room))
        qdii_members = {
            code: w for code, w in remaining.items() if is_qdii(markets.get(code, "cn"))
        }
        if qdii_members:
            fixed_qdii = sum(v for c, v in fixed.items() if is_qdii(markets.get(c, "cn")))
            room = max(max_qdii - fixed_qdii, 0.0)
            groups.append((_relative_excess(sum(qdii_members.values()), room), qdii_members, room))

        # 超限最严重的组；全部满足约束时按请求值收尾
        worst: tuple[float, dict[str, float], float] | None = None
        for group in groups:
            if group[0] > 1e-12 and (worst is None or group[0] > worst[0]):
                worst = group
        if worst is None:
            for code, w in remaining.items():
                fixed[code] = fixed.get(code, 0.0) + w
            remaining = {}
            break
        _excess, members, room = worst
        total = sum(members.values())
        factor = (room / total) if total > 0 else 0.0
        # 组内按请求占比整体收敛到额度并固定（同分同比例，互不相同）
        for code in members:
            final = remaining[code] * factor
            if final > 1e-12:
                fixed[code] = fixed.get(code, 0.0) + final
            del remaining[code]

    result = {code: round(w, 6) for code, w in fixed.items() if w > 1e-9}
    total = sum(result.values())
    if total > 1.0 + 1e-9:
        result = {code: round(weight / total, 6) for code, weight in result.items()}
    return result
