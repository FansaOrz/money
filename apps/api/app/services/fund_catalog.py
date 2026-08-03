"""全市场基金目录同步服务。

数据源：
- akshare ``fund_name_em()``：全市场公募基金（代码/拼音/全称/类型），
  是目录的主数据源，同步时幂等 upsert；
- akshare ``fund_open_fund_daily_em()``：当日有净值更新的开放式基金列表
  （可选），用于刷新 ``active`` 标记——在列表中的基金视为活跃，
  不在列表中的基金不直接置为不活跃（避免接口波动误伤），仅当
  ``mark_inactive=True`` 时才把缺失基金标记为不活跃。

派生字段：
- market：复用 ``quant_factors.classify_market`` 按名称关键词分类；
- family / share_class：按基金名称后缀（A/C/E 等份额类别）拆出基金家族，
  用于候选池的家族去重（同一基金只保留一个代表份额）。

幂等性：以 code 为唯一键 upsert，重复执行不产生重复行；
接口返回空/失败时不清空已有目录。
"""

from __future__ import annotations

import logging
import re
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import FundCatalogEntry
from app.services import quant_factors

logger = logging.getLogger(__name__)

# 份额类别后缀：名称以 "XXXA"/"XXXC" 等结尾时拆出家族名
_SHARE_CLASS_PATTERN = re.compile(r"^(?P<family>.+?)(?P<cls>[A-Z])$")
# 名称中明显的份额/联接标识，用于家族归一
_SHARE_TOKENS = (
    "A类", "C类", "B类", "D类", "E类", "H类", "I类", "O类", "Y类",
    "A份额", "C份额", "人民币", "美元现汇", "美元现钞",
)
_KNOWN_SHARE_CLASSES = {
    "A", "B", "C", "D", "E", "F", "H", "I", "K", "O", "R", "T", "U", "W", "X", "Y", "Z",
}


def split_family_share(name: str) -> tuple[str, str | None]:
    """把基金全称拆成 (家族名, 份额类别)。

    规则：
    - 去掉 "A类"/"C类"/"人民币" 等显式份额词；
    - 末尾单个大写字母且属于已知份额类别时视为份额后缀；
    - 无法识别时家族名即全称，份额为 None。
    """
    family = name.strip()
    share: str | None = None
    for token in _SHARE_TOKENS:
        if family.endswith(token):
            family = family[: -len(token)].strip()
            share = token[0] if token[0].isalpha() and len(token) > 1 else share
            break
    else:
        match = _SHARE_CLASS_PATTERN.match(family)
        if match and match.group("cls") in _KNOWN_SHARE_CLASSES and len(match.group("family")) >= 4:
            candidate = match.group("family").strip()
            # 避免把本身就以字母结尾的英文缩写（如 QDII、ETF、LOF、FOF）误拆
            if not candidate.endswith(("QDI", "ET", "LO", "FO")):
                family = candidate
                share = match.group("cls")
    # 去掉家族名尾部的连接符/括号残留
    family = family.rstrip("-–—·()（） ").strip()
    return family or name.strip(), share


def _fetch_fund_name_em() -> list[dict[str, Any]]:
    """调用 akshare fund_name_em，返回统一字段的字典列表。"""
    try:
        import akshare as ak
    except ImportError as exc:  # pragma: no cover - 环境无 akshare 时兜底
        raise RuntimeError("akshare 未安装，无法同步基金目录") from exc
    df = ak.fund_name_em()
    if df is None or df.empty:
        return []
    rows: list[dict[str, Any]] = []
    for record in df.to_dict("records"):
        code = str(record.get("基金代码") or "").strip()
        name = str(record.get("基金全称") or record.get("基金简称") or "").strip()
        if not code or not name:
            continue
        rows.append(
            {
                "code": code,
                "name": name,
                "pinyin_abbr": str(record.get("拼音缩写") or "").strip() or None,
                "pinyin_full": str(record.get("拼音全称") or "").strip() or None,
                "fund_type": str(record.get("基金类型") or "").strip() or None,
            }
        )
    return rows


def _fetch_active_codes() -> set[str]:
    """调用 akshare fund_open_fund_daily_em，返回当日有净值的基金代码集合。"""
    try:
        import akshare as ak
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("akshare 未安装，无法获取开放式基金净值列表") from exc
    df = ak.fund_open_fund_daily_em()
    if df is None or df.empty:
        return set()
    return {str(code).strip() for code in df["基金代码"].tolist() if str(code).strip()}


def _upsert_entry(db: Session, row: dict[str, Any]) -> tuple[FundCatalogEntry, bool]:
    """按 code 幂等 upsert 单条目录记录，返回 (条目, 是否新建)。"""
    entry = db.scalar(select(FundCatalogEntry).where(FundCatalogEntry.code == row["code"]))
    created = entry is None
    if entry is None:
        entry = FundCatalogEntry(code=row["code"], name=row["name"])
        db.add(entry)
    entry.name = row["name"]
    entry.pinyin_abbr = row["pinyin_abbr"]
    entry.pinyin_full = row["pinyin_full"]
    entry.fund_type = row["fund_type"]
    entry.market = quant_factors.classify_market(row["name"])
    family, share = split_family_share(row["name"])
    entry.family = family
    entry.share_class = share
    return entry, created


def sync_fund_catalog(
    db: Session,
    *,
    refresh_active: bool = False,
    mark_inactive: bool = False,
) -> dict[str, Any]:
    """同步全市场基金目录（幂等）。

    - refresh_active=True 时额外调用 fund_open_fund_daily_em，
      把出现在当日净值列表中的目录基金标记为 active；
    - mark_inactive=True 且 refresh_active 成功时，把不在列表中的基金
      标记为不活跃（默认关闭，避免接口波动误伤）。

    返回同步统计；数据源失败时抛 RuntimeError，不落库任何变更。
    """
    rows = _fetch_fund_name_em()
    if not rows:
        raise RuntimeError("fund_name_em 返回空数据，保留已有目录不变")

    inserted = 0
    updated = 0
    for row in rows:
        _entry, created = _upsert_entry(db, row)
        if created:
            inserted += 1
        else:
            updated += 1

    active_marked = 0
    inactive_marked = 0
    if refresh_active:
        active_codes = _fetch_active_codes()
        if active_codes:
            entries = db.scalars(select(FundCatalogEntry)).all()
            for entry in entries:
                is_active = entry.code in active_codes
                if is_active and not entry.active:
                    active_marked += 1
                if is_active:
                    entry.active = True
                elif mark_inactive and entry.active:
                    entry.active = False
                    inactive_marked += 1
        else:
            logger.warning("fund_open_fund_daily_em 返回空，跳过 active 状态刷新")

    db.commit()
    total = db.scalar(select(func.count(FundCatalogEntry.id))) or 0
    return {
        "total_rows": len(rows),
        "inserted": inserted,
        "updated": updated,
        "active_marked": active_marked,
        "inactive_marked": inactive_marked,
        "catalog_size": int(total),
    }


def get_catalog_stats(db: Session) -> dict[str, Any]:
    """目录统计：总量、active 分布、按 fund_type / market 的数量分布。"""
    total = db.scalar(select(func.count(FundCatalogEntry.id))) or 0
    active = db.scalar(
        select(func.count(FundCatalogEntry.id)).where(FundCatalogEntry.active.is_(True))
    ) or 0

    by_type_rows = db.execute(
        select(FundCatalogEntry.fund_type, func.count(FundCatalogEntry.id))
        .group_by(FundCatalogEntry.fund_type)
        .order_by(func.count(FundCatalogEntry.id).desc())
    ).all()
    by_market_rows = db.execute(
        select(FundCatalogEntry.market, func.count(FundCatalogEntry.id))
        .group_by(FundCatalogEntry.market)
        .order_by(func.count(FundCatalogEntry.id).desc())
    ).all()
    return {
        "total": int(total),
        "active": int(active),
        "inactive": int(total) - int(active),
        "by_type": {str(t or "未知"): int(c) for t, c in by_type_rows},
        "by_market": {str(m or "未知"): int(c) for m, c in by_market_rows},
    }
