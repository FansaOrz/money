"""主要市场指数行情服务。

数据源：AKShare 新浪系接口（统一日线 OHLC）：
- A 股指数：ak.stock_zh_index_daily(symbol="sh000001" / "sh000300")
- 港股指数：ak.stock_hk_index_daily_sina(symbol="HSI" / "HSTECH")
- 美股指数：ak.index_us_stock_sina(symbol=".INX" / ".IXIC")

所有指数统一存 date/open/high/low/close/volume/change_pct，
按 (index_id, trade_date) 幂等 upsert，可反复同步不产生重复行。
"""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

import pandas as pd
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.models import IndexQuote, MarketIndex

logger = logging.getLogger(__name__)

# 跟踪的主要市场指数：内部代码 -> 元数据
INDEX_DEFINITIONS: list[dict[str, str]] = [
    {
        "code": "SH000001",
        "name": "上证指数",
        "name_en": "SSE Composite",
        "market": "cn",
        "currency": "CNY",
        "source_symbol": "sh000001",
        "fetcher": "stock_zh_index_daily",
    },
    {
        "code": "CSI300",
        "name": "沪深300",
        "name_en": "CSI 300",
        "market": "cn",
        "currency": "CNY",
        "source_symbol": "sh000300",
        "fetcher": "stock_zh_index_daily",
    },
    {
        "code": "HSI",
        "name": "恒生指数",
        "name_en": "Hang Seng Index",
        "market": "hk",
        "currency": "HKD",
        "source_symbol": "HSI",
        "fetcher": "stock_hk_index_daily_sina",
    },
    {
        "code": "HSTECH",
        "name": "恒生科技指数",
        "name_en": "Hang Seng TECH",
        "market": "hk",
        "currency": "HKD",
        "source_symbol": "HSTECH",
        "fetcher": "stock_hk_index_daily_sina",
    },
    {
        "code": "SPX",
        "name": "标普500",
        "name_en": "S&P 500",
        "market": "us",
        "currency": "USD",
        "source_symbol": ".INX",
        "fetcher": "index_us_stock_sina",
    },
    {
        "code": "IXIC",
        "name": "纳斯达克综合指数",
        "name_en": "NASDAQ Composite",
        "market": "us",
        "currency": "USD",
        "source_symbol": ".IXIC",
        "fetcher": "index_us_stock_sina",
    },
]

# change_pct 有效范围（%），超出视为脏数据丢弃
_CHANGE_PCT_BOUND = Decimal("30")


def _to_decimal(value: Any) -> Decimal | None:
    """把数据源中的数值安全转换为 Decimal；失败返回 None。"""
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    try:
        return Decimal(str(value).strip().replace(",", ""))
    except (InvalidOperation, ValueError):
        return None


def _to_int(value: Any) -> int | None:
    decimal_value = _to_decimal(value)
    if decimal_value is None:
        return None
    try:
        return int(decimal_value)
    except (OverflowError, ValueError):
        return None


def _to_date(value: Any) -> date | None:
    """兼容 date/datetime/Timestamp/字符串 的日期解析。"""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()[:10]
    try:
        return date.fromisoformat(text)
    except ValueError:
        pass
    for fmt in ("%Y%m%d", "%Y/%m/%d"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def _compute_change_pct(close: Decimal, prev_close: Decimal | None) -> Decimal | None:
    """根据前一交易日收盘价计算日涨跌幅（%）。"""
    if prev_close is None or prev_close == 0:
        return None
    pct = (close - prev_close) / prev_close * 100
    pct = pct.quantize(Decimal("0.0001"))
    if abs(pct) > _CHANGE_PCT_BOUND:
        return None
    return pct


def _fetch_frame(fetcher: str, symbol: str) -> pd.DataFrame | None:
    """调用 AKShare 对应接口获取日线 DataFrame；失败返回 None。"""
    try:
        import akshare as ak
    except ImportError:
        logger.warning("未安装 akshare，无法同步指数行情")
        return None
    fn = getattr(ak, fetcher, None)
    if fn is None:
        logger.warning("akshare 缺少接口 %s", fetcher)
        return None
    try:
        frame = fn(symbol=symbol)
    except Exception as exc:  # 网络/限流/接口变更等，统一降级
        logger.warning("指数 %s（%s）行情抓取失败：%s", symbol, fetcher, exc)
        return None
    if frame is None or frame.empty:
        return None
    return frame


def parse_index_frame(frame: pd.DataFrame, days: int | None = None) -> list[dict]:
    """把 AKShare 日线 DataFrame 归一化为行情字典列表（按日期升序、去重）。

    输出字段：trade_date/open/high/low/close/volume/change_pct。
    change_pct 依据相邻收盘价计算。
    """
    rows: dict[date, dict] = {}
    for _, record in frame.iterrows():
        trade_date = _to_date(record.get("date"))
        close = _to_decimal(record.get("close"))
        if trade_date is None or close is None:
            continue
        rows[trade_date] = {
            "trade_date": trade_date,
            "open": _to_decimal(record.get("open")),
            "high": _to_decimal(record.get("high")),
            "low": _to_decimal(record.get("low")),
            "close": close,
            "volume": _to_int(record.get("volume")),
        }
    ordered = [rows[d] for d in sorted(rows)]
    if days is not None and days > 0:
        ordered = ordered[-days:]
    prev_close: Decimal | None = None
    for row in ordered:
        row["change_pct"] = _compute_change_pct(row["close"], prev_close)
        prev_close = row["close"]
    return ordered


def fetch_index_quotes(code: str, days: int | None = None) -> list[dict]:
    """抓取并归一化单个指数的日线行情。"""
    definition = next((item for item in INDEX_DEFINITIONS if item["code"] == code), None)
    if definition is None:
        raise ValueError(f"未知指数代码：{code}")
    frame = _fetch_frame(definition["fetcher"], definition["source_symbol"])
    if frame is None:
        return []
    return parse_index_frame(frame, days=days)


def ensure_indices(db: Session) -> list[MarketIndex]:
    """确保 market_indices 表中存在全部跟踪指数，返回指数列表。"""
    existing = {item.code: item for item in db.scalars(select(MarketIndex)).all()}
    indices: list[MarketIndex] = []
    for definition in INDEX_DEFINITIONS:
        index = existing.get(definition["code"])
        if index is None:
            index = MarketIndex(
                code=definition["code"],
                name=definition["name"],
                name_en=definition["name_en"],
                market=definition["market"],
                currency=definition["currency"],
                source_symbol=definition["source_symbol"],
            )
            db.add(index)
            db.flush()
        else:
            # 元数据允许随定义更新（名称/数据源代码等）
            index.name = definition["name"]
            index.name_en = definition["name_en"]
            index.market = definition["market"]
            index.currency = definition["currency"]
            index.source_symbol = definition["source_symbol"]
        indices.append(index)
    db.commit()
    return indices


def upsert_quotes(db: Session, index: MarketIndex, rows: list[dict]) -> int:
    """按 (index_id, trade_date) 幂等 upsert 行情，返回处理行数。"""
    if not rows:
        return 0
    dates = [row["trade_date"] for row in rows]
    existing = {
        quote.trade_date: quote
        for quote in db.scalars(
            select(IndexQuote).where(
                IndexQuote.index_id == index.id,
                IndexQuote.trade_date.in_(dates),
            )
        ).all()
    }
    for row in rows:
        quote = existing.get(row["trade_date"])
        if quote is None:
            quote = IndexQuote(index_id=index.id, trade_date=row["trade_date"], close=row["close"])
            db.add(quote)
        quote.open = row["open"]
        quote.high = row["high"]
        quote.low = row["low"]
        quote.close = row["close"]
        quote.volume = row["volume"]
        quote.change_pct = row["change_pct"]
    return len(rows)


def refresh_change_pct(db: Session, index: MarketIndex, from_date: date) -> None:
    """重算 from_date 当天的 change_pct，保证与前一交易日衔接。

    增量 upsert 时，新区间首行的 change_pct 可能基于抓取窗口内的前一行，
    而非库中真实前一交易日，这里统一校正。
    """
    db.flush()  # 确保刚 upsert 的行对下面的查询可见
    first = db.scalar(
        select(IndexQuote).where(
            IndexQuote.index_id == index.id,
            IndexQuote.trade_date == from_date,
        )
    )
    if first is None:
        return
    prev_close = db.scalar(
        select(IndexQuote.close)
        .where(
            IndexQuote.index_id == index.id,
            IndexQuote.trade_date < from_date,
        )
        .order_by(IndexQuote.trade_date.desc())
        .limit(1)
    )
    first.change_pct = _compute_change_pct(first.close, prev_close)


def sync_index_history(
    db: Session, days: int = 30, markets: list[str] | None = None
) -> dict[str, Any]:
    """同步跟踪指数的近期日线行情（幂等）。

    scheduler 与手动同步任务共用的入口；markets 过滤市场（如 ["us"]），
    缺省同步全部市场。
    """
    indices = ensure_indices(db)
    if markets is not None:
        allowed = set(markets)
        indices = [index for index in indices if index.market in allowed]
    updated = 0
    failed = 0
    total_rows = 0
    errors: list[str] = []
    for index in indices:
        try:
            rows = fetch_index_quotes(index.code, days=days)
        except Exception as exc:  # 防御：单个指数失败不影响其他
            logger.exception("指数 %s 同步失败", index.code)
            errors.append(f"{index.code}: {exc}")
            failed += 1
            continue
        if not rows:
            failed += 1
            errors.append(f"{index.code}: 数据源返回空")
            continue
        total_rows += upsert_quotes(db, index, rows)
        refresh_change_pct(db, index, rows[0]["trade_date"])
        updated += 1
    db.commit()
    return {
        "synced_at": datetime.now(UTC),
        "total_indices": len(indices),
        "updated_indices": updated,
        "failed": failed,
        "rows": total_rows,
        "errors": errors,
    }


def list_index_summaries(db: Session) -> list[dict[str, Any]]:
    """全部指数的最新行情摘要（无行情的指数也会返回，字段为 None）。"""
    indices = ensure_indices(db)
    summaries: list[dict[str, Any]] = []
    for index in indices:
        latest = db.scalars(
            select(IndexQuote)
            .where(IndexQuote.index_id == index.id)
            .order_by(IndexQuote.trade_date.desc())
            .limit(1)
        ).first()
        summaries.append(
            {
                "code": index.code,
                "name": index.name,
                "name_en": index.name_en,
                "market": index.market,
                "currency": index.currency,
                "latest_date": latest.trade_date if latest else None,
                "close": latest.close if latest else None,
                "change_pct": latest.change_pct if latest else None,
                "volume": latest.volume if latest else None,
            }
        )
    return summaries


def get_index_history(db: Session, code: str, days: int = 90) -> tuple[MarketIndex, list[IndexQuote]] | None:
    """查询单个指数近 days 个交易日的日线（按日期升序）。指数不存在返回 None。"""
    index = db.scalar(select(MarketIndex).where(MarketIndex.code == code.upper()))
    if index is None:
        return None
    ids = select(IndexQuote.id).where(IndexQuote.index_id == index.id)
    if days > 0:
        recent_ids = (
            select(IndexQuote.id)
            .where(IndexQuote.index_id == index.id)
            .order_by(IndexQuote.trade_date.desc())
            .limit(days)
            .scalar_subquery()
        )
        ids = select(IndexQuote.id).where(IndexQuote.id.in_(recent_ids))
    quotes = db.scalars(
        select(IndexQuote).where(IndexQuote.id.in_(ids)).order_by(IndexQuote.trade_date)
    ).all()
    return index, list(quotes)


def clear_index_quotes(db: Session, index: MarketIndex) -> None:
    """清空指定指数行情（仅供维护/测试使用）。"""
    db.execute(delete(IndexQuote).where(IndexQuote.index_id == index.id))
    db.commit()
