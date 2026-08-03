"""候选池构建服务：从全市场目录筛选、分层配额、家族去重，生成核心候选池。

流程（只建池，不触发全历史净值回填）：
1. 过滤：active、基金类型（股票型/混合型/指数型/QDII 等权益类入选；
   货币型默认排除；债券型/黄金等进观察层）、排除名称含"联接""LOF""定开"
   等非主份额/特殊运作形式（可通过参数放宽）；
2. 家族去重：同一 family 只保留一个代表份额（优先无份额后缀的主基金，
   其次 A 类，再次按代码升序）；
3. 分层：tier1 核心权益（cn/cn_300）、tier2 次级权益（hk/us 等）、
   tier3 观察（黄金/债券/货币/其他海外）；
4. 配额：max_size（默认 800，钳制在 500~1000）按
   tier1:tier2:tier3 = 70%:25%:5% 分配，剩余额度顺延；
5. nav_ready / nav_samples：以库内 FundNav 行数衡量净值就绪程度
   （>=MIN_NAV_SAMPLES 视为就绪），仅作标记，不阻塞建池——
   全历史回填由后续任务按池内代码调度执行。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import (
    CandidatePool,
    CandidatePoolMember,
    FundCatalogEntry,
    FundNav,
    Instrument,
    InstrumentType,
)
from app.services import quant_factors

# 池规模约束
DEFAULT_MAX_SIZE = 800
MIN_MAX_SIZE = 500
MAX_MAX_SIZE = 1000

# 分层配额（tier1 / tier2 / tier3）
TIER_QUOTAS: dict[int, float] = {1: 0.70, 2: 0.25, 3: 0.05}

# 12-1 动量与 V2 信号至少需要 253 个净值点；覆盖状态使用同一真实门槛。
MIN_NAV_SAMPLES = 253

# 入选核心/观察层的基金类型（东财一级分类关键词）
_EQUITY_TYPES = ("股票型", "混合型", "指数型", "QDII", "股票指数", "混合-")
_OBSERVE_TYPES = ("债券型", "货币型", "商品", "另类")
# 默认排除的运作形式（联接基金与被联接基金重复、定开/封闭流动性差）
_DEFAULT_EXCLUDE_KEYWORDS = ("联接", "定开", "封闭", "持有期", "滚动持有")


@dataclass
class PoolBuildParams:
    """建池参数。"""

    max_size: int = DEFAULT_MAX_SIZE
    include_types: tuple[str, ...] | None = None  # None 用默认权益类
    exclude_keywords: tuple[str, ...] = _DEFAULT_EXCLUDE_KEYWORDS
    only_active: bool = True
    name: str | None = None

    def clamped_max_size(self) -> int:
        return max(MIN_MAX_SIZE, min(MAX_MAX_SIZE, int(self.max_size)))


def _is_equity_type(fund_type: str | None) -> bool:
    if not fund_type:
        return False
    return any(token in fund_type for token in _EQUITY_TYPES)


def _is_observe_type(fund_type: str | None) -> bool:
    if not fund_type:
        return False
    return any(token in fund_type for token in _OBSERVE_TYPES)


def filter_entries(
    entries: list[FundCatalogEntry],
    params: PoolBuildParams,
) -> tuple[list[FundCatalogEntry], dict[str, int]]:
    """过滤目录条目。返回 (通过过滤的条目, 各环节剔除统计)。"""
    excluded: dict[str, int] = {
        "inactive": 0,
        "type": 0,
        "keyword": 0,
    }
    passed: list[FundCatalogEntry] = []
    for entry in entries:
        if params.only_active and not entry.active:
            excluded["inactive"] += 1
            continue
        if not (_is_equity_type(entry.fund_type) or _is_observe_type(entry.fund_type)):
            excluded["type"] += 1
            continue
        if params.exclude_keywords and any(
            keyword in entry.name for keyword in params.exclude_keywords
        ):
            excluded["keyword"] += 1
            continue
        passed.append(entry)
    return passed, excluded


def dedupe_families(entries: list[FundCatalogEntry]) -> tuple[list[FundCatalogEntry], int]:
    """家族去重：同一 family 保留一个代表份额。

    代表份额优先级：无份额后缀（主基金）> A 类 > 代码升序。
    返回 (去重后条目, 剔除数)。
    """
    def _priority(entry: FundCatalogEntry) -> tuple[int, str]:
        if entry.share_class is None:
            return (0, entry.code)
        if entry.share_class == "A":
            return (1, entry.code)
        return (2, entry.code)

    best: dict[str, FundCatalogEntry] = {}
    for entry in entries:
        family = entry.family or entry.name
        current = best.get(family)
        if current is None or _priority(entry) < _priority(current):
            best[family] = entry
    removed = len(entries) - len(best)
    return list(best.values()), removed


def assign_tier(entry: FundCatalogEntry) -> int:
    """按市场与基金类型分层：1 核心权益 / 2 次级权益 / 3 观察。"""
    if _is_observe_type(entry.fund_type):
        return 3
    market = entry.market or "cn"
    if not quant_factors.is_equity_market(market):
        return 3
    if market in ("cn", "cn_300"):
        return 1
    return 2


def apply_tier_quotas(
    entries: list[FundCatalogEntry], max_size: int
) -> tuple[list[tuple[FundCatalogEntry, int]], dict[str, int]]:
    """按分层配额截取。返回 ([(条目, tier)], 各层实际取用数)。"""
    by_tier: dict[int, list[FundCatalogEntry]] = {1: [], 2: [], 3: []}
    for entry in entries:
        by_tier[assign_tier(entry)].append(entry)
    # 层内确定性排序：先无后缀主基金，再按代码升序
    for tier_entries in by_tier.values():
        tier_entries.sort(key=lambda e: (e.share_class is not None, e.code))

    quotas = {tier: int(max_size * ratio) for tier, ratio in TIER_QUOTAS.items()}
    picked: list[tuple[FundCatalogEntry, int]] = []
    counts: dict[str, int] = {}
    overflow: list[tuple[FundCatalogEntry, int]] = []
    for tier in (1, 2, 3):
        candidates = by_tier[tier]
        quota = quotas[tier]
        for entry in candidates[:quota]:
            picked.append((entry, tier))
        for entry in candidates[quota:]:
            overflow.append((entry, tier))
        counts[f"tier{tier}"] = min(len(candidates), quota)

    # 配额未用完时，用溢出条目按层优先（tier1>tier2>tier3）顺延补齐
    remaining = max_size - len(picked)
    if remaining > 0 and overflow:
        overflow.sort(key=lambda item: (item[1], item[0].code))
        for entry, tier in overflow[:remaining]:
            picked.append((entry, tier))
            counts[f"tier{tier}"] = counts.get(f"tier{tier}", 0) + 1
    return picked, counts


def _nav_sample_counts(db: Session, codes: list[str]) -> dict[str, int]:
    """统计各代码在库内的净值样本数（通过 instruments 关联 fund_navs）。"""
    if not codes:
        return {}
    rows = db.execute(
        select(Instrument.code, func.count(FundNav.id))
        .join(FundNav, FundNav.instrument_id == Instrument.id)
        .where(Instrument.code.in_(codes))
        .group_by(Instrument.code)
    ).all()
    return {code: int(count) for code, count in rows}


def build_candidate_pool(db: Session, params: PoolBuildParams) -> CandidatePool:
    """构建候选池：过滤 → 家族去重 → 分层配额 → 落库。

    只建池并标记 nav_ready，不触发全历史净值回填。
    """
    max_size = params.clamped_max_size()
    entries = db.scalars(select(FundCatalogEntry).order_by(FundCatalogEntry.code)).all()
    filtered, excluded = filter_entries(list(entries), params)
    deduped, family_removed = dedupe_families(filtered)
    picked, tier_counts = apply_tier_quotas(deduped, max_size)

    codes = [entry.code for entry, _tier in picked]
    # 入池成员需要成为可同步的 Instrument；目录本身仍保留全部 27k 条，只有
    # 研究池成员才创建 instrument/FundNav 缓存，避免全量历史立即膨胀。
    existing_codes = set(
        db.scalars(select(Instrument.code).where(Instrument.code.in_(codes))).all()
    )
    for entry, _tier in picked:
        if entry.code not in existing_codes:
            db.add(
                Instrument(
                    code=entry.code,
                    name=entry.name,
                    type=InstrumentType.FUND,
                    currency="CNY",
                )
            )
    db.flush()
    nav_counts = _nav_sample_counts(db, codes)

    pool = CandidatePool(
        name=params.name or f"核心候选池（{max_size}）",
        max_size=max_size,
        status="ready",
        params=json.dumps(
            {
                "max_size": max_size,
                "only_active": params.only_active,
                "exclude_keywords": list(params.exclude_keywords),
                "tier_quotas": TIER_QUOTAS,
                "excluded": excluded,
                "family_removed": family_removed,
                "tier_counts": tier_counts,
                "catalog_total": len(entries),
            },
            ensure_ascii=False,
        ),
        notes=(
            f"目录 {len(entries)} → 过滤剔除 {sum(excluded.values())} → "
            f"家族去重剔除 {family_removed} → 入池 {len(picked)}"
        ),
    )
    db.add(pool)
    db.flush()  # 先拿到 pool.id 再写 members

    # 排序：tier 升序，层内按代码升序，rank 从 1 开始
    picked.sort(key=lambda item: (item[1], item[0].code))
    for rank, (entry, tier) in enumerate(picked, start=1):
        samples = nav_counts.get(entry.code, 0)
        db.add(
            CandidatePoolMember(
                pool_id=pool.id,
                code=entry.code,
                name=entry.name,
                fund_type=entry.fund_type,
                market=entry.market,
                family=entry.family,
                share_class=entry.share_class,
                tier=tier,
                rank=rank,
                status="active",
                nav_samples=samples,
                nav_ready=samples >= MIN_NAV_SAMPLES,
            )
        )
    pool.member_count = len(picked)
    db.commit()
    db.refresh(pool)
    return pool


def list_pools(db: Session, limit: int = 20) -> list[CandidatePool]:
    """按创建时间倒序列出候选池。"""
    return list(
        db.scalars(
            select(CandidatePool).order_by(CandidatePool.created_at.desc()).limit(limit)
        ).all()
    )


def get_pool(db: Session, pool_id: int) -> CandidatePool | None:
    """按 id 取候选池（含成员）。"""
    return db.get(CandidatePool, pool_id)


def refresh_member_nav_status(db: Session, pool_id: int) -> int:
    """回填完成后刷新池内成员的 nav_samples / nav_ready，返回更新行数。"""
    pool = db.get(CandidatePool, pool_id)
    if pool is None:
        return 0
    codes = [member.code for member in pool.members]
    counts = _nav_sample_counts(db, codes)
    updated = 0
    for member in pool.members:
        samples = counts.get(member.code, 0)
        ready = samples >= MIN_NAV_SAMPLES
        if samples != member.nav_samples or ready != member.nav_ready:
            member.nav_samples = samples
            member.nav_ready = ready
            updated += 1
    db.commit()
    return updated


def pool_summary(pool: CandidatePool) -> dict[str, Any]:
    """池概要统计：按层、按市场、nav_ready 数量。"""
    tier_counts: dict[int, int] = {}
    market_counts: dict[str, int] = {}
    nav_ready = 0
    for member in pool.members:
        tier_counts[member.tier] = tier_counts.get(member.tier, 0) + 1
        market = member.market or "未知"
        market_counts[market] = market_counts.get(market, 0) + 1
        if member.nav_ready:
            nav_ready += 1
    return {
        "tier_counts": {str(k): v for k, v in sorted(tier_counts.items())},
        "market_counts": dict(sorted(market_counts.items(), key=lambda kv: -kv[1])),
        "nav_ready_count": nav_ready,
    }
