"""资讯抓取与查询服务。

数据源策略（均无需新增第三方依赖）：
- 公开 RSS（东方财富等）通过 urllib + 标准库 XML 解析抓取；
- AKShare 若环境中已安装，则作为补充数据源（个股/基金新闻），
  未安装或调用失败时优雅降级，仅记录日志并跳过。

绝不伪造数据：任何数据源抓取失败都返回空列表并标记降级状态。
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from urllib.error import URLError
from urllib.request import Request, urlopen
from xml.etree import ElementTree

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Instrument, NewsItem

logger = logging.getLogger(__name__)

# 全局市场快讯使用的关联标记：related_codes 为空即视为 market 范围
MARKET_SCOPE = "market"
RELATED_SCOPE = "related"

# 无需新增依赖的公开 RSS 源（财经快讯/要闻）。
# 任一源失败不影响其他源，整体可在全部失败时降级为空。
DEFAULT_RSS_FEEDS: tuple[tuple[str, str], ...] = (
    ("eastmoney_rss", "https://rss.eastmoney.com/rss_partener.xml"),
    ("sina_finance", "https://feed.mix.sina.com.cn/api/roll/get?pageid=153&lid=2516&num=20&callback="),
)

USER_AGENT = "Mozilla/5.0 money-personal-dashboard/0.1"

# 最近一次同步状态（进程内，供 API 读取；不落库避免额外表）
_last_sync_status: dict = {
    "synced_at": None,
    "fetched": 0,
    "inserted": 0,
    "skipped": 0,
    "degraded": False,
    "message": None,
}


@dataclass
class RawNews:
    """抓取到的原始资讯条目。"""

    source: str
    title: str
    summary: str | None = None
    url: str | None = None
    published_at: datetime | None = None
    # 数据源自身给出的标的代码（若有），其余由关键词匹配补充
    codes: list[str] = field(default_factory=list)


def get_last_sync_status() -> dict:
    """返回最近一次同步状态（浅拷贝）。"""
    return dict(_last_sync_status)


def _set_last_sync_status(**kwargs: object) -> None:
    _last_sync_status.update(kwargs)


def _content_hash(source: str, title: str, url: str | None) -> str:
    """根据来源 + 标题 + 链接生成去重指纹。"""
    digest = hashlib.sha256()
    digest.update(source.encode("utf-8"))
    digest.update(b"\x00")
    digest.update(title.strip().encode("utf-8"))
    digest.update(b"\x00")
    digest.update((url or "").strip().encode("utf-8"))
    return digest.hexdigest()


def _request_text(url: str, timeout: int = 15) -> str | None:
    """发起 GET 请求并返回文本；失败返回 None（优雅降级）。"""
    request = Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urlopen(request, timeout=timeout) as response:
            raw = response.read()
        # 尝试常见中文编码
        for encoding in ("utf-8", "gb18030"):
            try:
                return raw.decode(encoding)
            except UnicodeDecodeError:
                continue
        return raw.decode("utf-8", errors="ignore")
    except (OSError, URLError, TimeoutError, ValueError) as exc:
        logger.warning("资讯请求失败 %s: %s", url, exc)
        return None


def _parse_pub_date(value: str | None) -> datetime | None:
    """解析 RSS pubDate（RFC 822）或 ISO 日期。"""
    if not value:
        return None
    value = value.strip()
    try:
        parsed = parsedate_to_datetime(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed
    except (TypeError, ValueError):
        pass
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            parsed = datetime.strptime(value[: len(fmt) + 6], fmt)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=UTC)
            return parsed
        except ValueError:
            continue
    return None


def _strip_html(text: str | None) -> str | None:
    if not text:
        return None
    cleaned = re.sub(r"<[^>]+>", "", text)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned or None


def parse_rss(text: str, source: str) -> list[RawNews]:
    """解析 RSS 2.0 / Atom 文本为原始资讯列表。解析失败返回空。"""
    try:
        root = ElementTree.fromstring(text)
    except ElementTree.ParseError as exc:
        logger.warning("RSS 解析失败（%s）: %s", source, exc)
        return []

    items: list[RawNews] = []
    # RSS 2.0: <channel><item>...
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        if not title:
            continue
        items.append(
            RawNews(
                source=source,
                title=title,
                summary=_strip_html(item.findtext("description")),
                url=(item.findtext("link") or "").strip() or None,
                published_at=_parse_pub_date(item.findtext("pubDate")),
            )
        )
    if items:
        return items

    # Atom: {http://www.w3.org/2005/Atom}entry
    atom_ns = "{http://www.w3.org/2005/Atom}"
    for entry in root.iter(f"{atom_ns}entry"):
        title = (entry.findtext(f"{atom_ns}title") or "").strip()
        if not title:
            continue
        link = None
        for link_el in entry.findall(f"{atom_ns}link"):
            link = link_el.get("href") or link
        published = entry.findtext(f"{atom_ns}published") or entry.findtext(f"{atom_ns}updated")
        items.append(
            RawNews(
                source=source,
                title=title,
                summary=_strip_html(entry.findtext(f"{atom_ns}summary")),
                url=link,
                published_at=_parse_pub_date(published),
            )
        )
    return items


def parse_sina_roll(text: str, source: str) -> list[RawNews]:
    """解析新浪滚动快讯 JSON（JSONP 包裹的 JSON）。"""
    body = text.strip()
    # 去掉 JSONP 包裹 callback(...)
    if body.startswith("(") or (not body.startswith("{") and "(" in body):
        start = body.find("(")
        end = body.rfind(")")
        if start != -1 and end != -1 and end > start:
            body = body[start + 1 : end]
    try:
        payload = json.loads(body)
    except ValueError as exc:
        logger.warning("新浪快讯 JSON 解析失败: %s", exc)
        return []
    data = (payload.get("result") or {}).get("data") or []
    items: list[RawNews] = []
    for row in data:
        title = (row.get("title") or "").strip()
        if not title:
            continue
        published: datetime | None = None
        ctime = row.get("ctime")
        if ctime:
            try:
                published = datetime.fromtimestamp(int(ctime), tz=UTC)
            except (TypeError, ValueError, OSError):
                published = None
        items.append(
            RawNews(
                source=source,
                title=title,
                summary=_strip_html(row.get("summary") or row.get("intro")),
                url=row.get("url") or row.get("docurl"),
                published_at=published,
            )
        )
    return items


def fetch_rss_news(feeds: tuple[tuple[str, str], ...] = DEFAULT_RSS_FEEDS) -> list[RawNews]:
    """抓取所有 RSS/JSON 源，任一源失败不影响其余源。"""
    results: list[RawNews] = []
    for source, url in feeds:
        text = _request_text(url)
        if text is None:
            continue
        if "sina" in source:
            results.extend(parse_sina_roll(text, source))
        else:
            results.extend(parse_rss(text, source))
    return results


def _fetch_akshare_news(codes: list[str]) -> list[RawNews]:
    """可选数据源：若环境安装了 AKShare，则抓取个股/基金新闻。

    未安装或调用失败时返回空列表（优雅降级）。
    """
    try:
        import akshare as ak  # type: ignore[import-not-found]
    except ImportError:
        logger.info("AKShare 未安装，跳过个股资讯源")
        return []

    results: list[RawNews] = []
    for code in codes:
        try:
            df = ak.stock_news_em(symbol=code)
        except Exception as exc:  # AKShare 可能抛出任意网络/解析异常
            logger.warning("AKShare 抓取 %s 资讯失败: %s", code, exc)
            continue
        if df is None or getattr(df, "empty", True):
            continue
        for _, row in df.head(20).iterrows():
            title = str(row.get("新闻标题") or "").strip()
            if not title:
                continue
            published = None
            raw_time = row.get("发布时间")
            if raw_time:
                try:
                    published = datetime.strptime(str(raw_time), "%Y-%m-%d %H:%M:%S").replace(
                        tzinfo=UTC
                    )
                except ValueError:
                    published = None
            results.append(
                RawNews(
                    source="akshare_stock_news",
                    title=title,
                    summary=_strip_html(str(row.get("新闻内容") or "")[:300]) or None,
                    url=str(row.get("新闻链接") or "") or None,
                    published_at=published,
                    codes=[code],
                )
            )
    return results


def _holding_keywords(db: Session) -> dict[str, list[str]]:
    """构建"代码 -> 关键词列表"映射，用于相关资讯匹配。

    关键词来自：
    - 当前持仓基金名称（及其去掉常见后缀的简称）；
    - 若未来存在 fund_holdings 表，则附加基金重仓股名称（兼容查询，
      表不存在时静默跳过，退回基金名称匹配）。
    """
    keywords: dict[str, list[str]] = {}
    instruments = db.scalars(select(Instrument).order_by(Instrument.code)).all()
    for instrument in instruments:
        names = {instrument.name}
        # 生成简称：去掉"股票/混合/债券/指数"等后缀词，提高命中率
        short = re.sub(r"(股票型|股票|混合型|混合|债券型|债券|指数型|指数|QDII|LOF|ETF|A类|C类|A|C)$", "", instrument.name)
        short = short.strip()
        if short and len(short) >= 2:
            names.add(short)
        keywords[instrument.code] = sorted(names, key=len, reverse=True)

    # 兼容未来 fund_holdings 表：存在则附加重仓股名称作为关键词
    try:
        from sqlalchemy import inspect as sa_inspect, text as sa_text

        inspector = sa_inspect(db.get_bind())
        if "fund_holdings" in inspector.get_table_names():
            # 当前表通过 instrument_id 关联基金代码。
            rows = db.execute(
                sa_text(
                    "SELECT i.code AS fund_code, h.stock_name, h.stock_code "
                    "FROM fund_holdings h JOIN instruments i ON i.id = h.instrument_id"
                )
            ).all()
            for fund_code, stock_name, stock_code in rows:
                if fund_code in keywords and stock_name:
                    keywords[fund_code].append(str(stock_name))
                if stock_code:
                    keywords.setdefault(str(stock_code), []).append(str(stock_name or stock_code))
    except Exception as exc:  # 任何反射/SQL 异常都不应影响主流程
        logger.info("fund_holdings 兼容查询跳过: %s", exc)
    return keywords


def _match_codes(title: str, summary: str | None, keywords: dict[str, list[str]]) -> list[str]:
    """根据标题/摘要中的关键词匹配关联标的代码。"""
    text = f"{title} {summary or ''}"
    matched: list[str] = []
    for code, names in keywords.items():
        for name in names:
            if name and name in text:
                matched.append(code)
                break
    return matched


def save_news_items(db: Session, raw_items: list[RawNews], keywords: dict[str, list[str]]) -> dict[str, int]:
    """去重后批量入库（含批内去重）。返回统计信息。"""
    fetched = len(raw_items)
    inserted = 0
    skipped = 0
    seen_hashes: set[str] = set()
    for raw in raw_items:
        content_hash = _content_hash(raw.source, raw.title, raw.url)
        if content_hash in seen_hashes:
            skipped += 1
            continue
        exists = db.scalar(
            select(NewsItem.id).where(NewsItem.content_hash == content_hash).limit(1)
        )
        if exists is not None:
            skipped += 1
            continue
        seen_hashes.add(content_hash)
        codes = list(raw.codes) or _match_codes(raw.title, raw.summary, keywords)
        db.add(
            NewsItem(
                source=raw.source,
                title=raw.title[:500],
                summary=(raw.summary[:2000] if raw.summary else None),
                url=(raw.url[:1000] if raw.url else None),
                published_at=raw.published_at,
                related_codes=",".join(sorted(set(codes))) if codes else None,
                content_hash=content_hash,
            )
        )
        inserted += 1
    db.commit()
    return {"fetched": fetched, "inserted": inserted, "skipped": skipped}


def sync_news(db: Session, include_akshare: bool = True) -> dict:
    """执行一次完整同步：抓取 -> 关键词匹配 -> 去重入库。

    返回同步结果；任何源失败均不会抛出，整体结果反映降级状态。
    """
    errors: list[str] = []
    keywords = _holding_keywords(db)

    raw_items: list[RawNews] = []
    try:
        raw_items.extend(fetch_rss_news())
    except Exception as exc:  # 防御：抓取层已处理，此处兜底
        logger.warning("RSS 抓取整体失败: %s", exc)
        errors.append(f"rss: {exc}")

    if include_akshare:
        try:
            # stock_news_em 仅接受 A 股代码，并限制请求量避免数据源限流。
            # 基金代码和 A 股代码都为 6 位，不能直接从关键词键区分。
            # 这里暂不逐代码抓取，避免把基金代码误传给个股资讯接口；市场 RSS 仍正常同步。
            raw_items.extend(_fetch_akshare_news([]))
        except Exception as exc:
            logger.warning("AKShare 抓取整体失败: %s", exc)
            errors.append(f"akshare: {exc}")

    if not raw_items:
        message = "未抓取到任何资讯（数据源不可用或无更新），已优雅降级"
        _set_last_sync_status(
            synced_at=datetime.now(UTC),
            fetched=0,
            inserted=0,
            skipped=0,
            degraded=True,
            message=message,
        )
        return {
            "synced_at": _last_sync_status["synced_at"],
            "fetched": 0,
            "inserted": 0,
            "skipped": 0,
            "degraded": True,
            "message": message,
            "errors": errors,
        }

    stats = save_news_items(db, raw_items, keywords)
    _set_last_sync_status(
        synced_at=datetime.now(UTC),
        fetched=stats["fetched"],
        inserted=stats["inserted"],
        skipped=stats["skipped"],
        degraded=False,
        message=None,
    )
    return {
        "synced_at": _last_sync_status["synced_at"],
        **stats,
        "degraded": False,
        "message": None,
        "errors": errors,
    }


def list_news(
    db: Session,
    scope: str = RELATED_SCOPE,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[NewsItem], int]:
    """按范围查询资讯。

    - related: related_codes 非空且与任一持仓基金代码有交集的资讯；
    - market: related_codes 为空的全局快讯。
    """
    base = select(NewsItem)
    count_q = select(func.count(NewsItem.id))

    if scope == MARKET_SCOPE:
        cond = NewsItem.related_codes.is_(None)
        base = base.where(cond)
        count_q = count_q.where(cond)
        total = db.scalar(count_q) or 0
        rows = list(
            db.scalars(
                base.order_by(NewsItem.published_at.desc().nulls_last(), NewsItem.id.desc())
                .limit(limit)
                .offset(offset)
            ).all()
        )
        return rows, total

    # related: 仅返回与当前持仓/自选基金代码有交集的资讯
    codes = set(db.scalars(select(Instrument.code)).all())
    if not codes:
        return [], 0
    cond = NewsItem.related_codes.is_not(None)
    base = base.where(cond)

    # 逗号分隔字段无法直接用 SQL 做集合交集，取较新一批在内存中过滤
    rows = list(
        db.scalars(
            base.order_by(NewsItem.published_at.desc().nulls_last(), NewsItem.id.desc()).limit(500)
        ).all()
    )
    filtered = [
        item
        for item in rows
        if item.related_codes and codes.intersection(item.related_codes.split(","))
    ]
    total = len(filtered)
    return filtered[offset : offset + limit], total
