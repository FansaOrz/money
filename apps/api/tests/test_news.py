"""资讯服务与接口测试。

覆盖：
- RSS / 新浪 JSON 解析；
- content_hash 去重；
- related / market 两种范围的查询；
- 关键词（基金名称）匹配；
- 数据源不可用时的优雅降级（不伪造新闻）；
- 路由注册与响应结构。
"""

from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models import Instrument, InstrumentType, NewsItem
from app.services import news as news_service
from app.services.news import (
    RawNews,
    parse_rss,
    parse_sina_roll,
    save_news_items,
    sync_news,
)

RSS_SAMPLE = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>财经要闻</title>
    <item>
      <title>易方达消费行业基金一季度大幅加仓白酒股</title>
      <link>https://example.com/news/1</link>
      <pubDate>Wed, 29 Jul 2026 10:00:00 +0800</pubDate>
      <description>&lt;p&gt;基金季报披露&lt;/p&gt;</description>
    </item>
    <item>
      <title>央行开展 500 亿元逆回购操作</title>
      <link>https://example.com/news/2</link>
      <pubDate>Wed, 29 Jul 2026 09:00:00 +0800</pubDate>
    </item>
    <item>
      <title>易方达消费行业基金一季度大幅加仓白酒股</title>
      <link>https://example.com/news/1</link>
      <pubDate>Wed, 29 Jul 2026 10:00:00 +0800</pubDate>
    </item>
  </channel>
</rss>
"""

SINA_SAMPLE = '({"result":{"data":[{"title":"沪指午后翻红","url":"https://example.com/a","ctime":"1785000000","summary":"两市成交额放大"},{"title":"基金发行回暖","url":"https://example.com/b","ctime":"1785000100"}]}})'


def _add_instrument(db: Session, code: str = "110022", name: str = "易方达消费行业股票") -> Instrument:
    instrument = Instrument(code=code, name=name, type=InstrumentType.FUND)
    db.add(instrument)
    db.commit()
    return instrument


class TestParsers:
    def test_parse_rss_extracts_items(self) -> None:
        items = parse_rss(RSS_SAMPLE, "eastmoney_rss")
        assert len(items) == 3
        first = items[0]
        assert first.source == "eastmoney_rss"
        assert "易方达消费" in first.title
        assert first.url == "https://example.com/news/1"
        assert first.published_at is not None
        assert first.published_at.tzinfo is not None
        # HTML 标签被剥离
        assert first.summary is not None and "<" not in first.summary

    def test_parse_rss_invalid_returns_empty(self) -> None:
        assert parse_rss("not xml at all", "x") == []
        assert parse_rss("", "x") == []

    def test_parse_sina_roll_jsonp(self) -> None:
        items = parse_sina_roll(SINA_SAMPLE, "sina_finance")
        assert len(items) == 2
        assert items[0].title == "沪指午后翻红"
        assert items[0].published_at is not None

    def test_parse_sina_roll_invalid_returns_empty(self) -> None:
        assert parse_sina_roll("not json", "sina_finance") == []


class TestSaveAndDedupe:
    def test_save_dedupes_by_content_hash(self, db_session: Session) -> None:
        raw = [
            RawNews(source="rss", title="新闻A", url="https://x/1"),
            RawNews(source="rss", title="新闻A", url="https://x/1"),  # 完全重复
            RawNews(source="rss", title="新闻B", url="https://x/2"),
        ]
        stats = save_news_items(db_session, raw, keywords={})
        assert stats == {"fetched": 3, "inserted": 2, "skipped": 1}
        # 再次保存同一批，全部跳过
        stats2 = save_news_items(db_session, raw, keywords={})
        assert stats2["inserted"] == 0
        assert stats2["skipped"] == 3
        assert db_session.query(NewsItem).count() == 2

    def test_keyword_matching_links_fund_code(self, db_session: Session) -> None:
        _add_instrument(db_session)
        raw = [RawNews(source="rss", title="易方达消费行业股票基金净值上涨")]
        save_news_items(db_session, raw, keywords=news_service._holding_keywords(db_session))
        item = db_session.query(NewsItem).one()
        assert item.related_codes == "110022"

    def test_no_keyword_match_leaves_related_codes_null(self, db_session: Session) -> None:
        _add_instrument(db_session)
        raw = [RawNews(source="rss", title="央行公开市场操作")]
        save_news_items(db_session, raw, keywords=news_service._holding_keywords(db_session))
        item = db_session.query(NewsItem).one()
        assert item.related_codes is None


class TestListNews:
    def _seed(self, db: Session) -> None:
        _add_instrument(db)
        db.add_all(
            [
                NewsItem(
                    source="rss",
                    title="易方达消费行业相关",
                    url="https://x/1",
                    published_at=datetime(2026, 7, 29, 10, tzinfo=UTC),
                    related_codes="110022",
                    content_hash="h1",
                ),
                NewsItem(
                    source="rss",
                    title="全市场快讯",
                    url="https://x/2",
                    published_at=datetime(2026, 7, 29, 9, tzinfo=UTC),
                    related_codes=None,
                    content_hash="h2",
                ),
                NewsItem(
                    source="rss",
                    title="其他基金资讯",
                    url="https://x/3",
                    published_at=datetime(2026, 7, 29, 8, tzinfo=UTC),
                    related_codes="000001",
                    content_hash="h3",
                ),
            ]
        )
        db.commit()

    def test_related_scope_returns_only_matching(self, db_session: Session) -> None:
        self._seed(db_session)
        items, total = news_service.list_news(db_session, scope="related")
        assert total == 1
        assert items[0].related_codes == "110022"

    def test_market_scope_returns_only_untagged(self, db_session: Session) -> None:
        self._seed(db_session)
        items, total = news_service.list_news(db_session, scope="market")
        assert total == 1
        assert items[0].related_codes is None

    def test_related_scope_empty_without_instruments(self, db_session: Session) -> None:
        db_session.add(
            NewsItem(
                source="rss",
                title="资讯",
                url="https://x/9",
                related_codes="110022",
                content_hash="hx",
            )
        )
        db_session.commit()
        items, total = news_service.list_news(db_session, scope="related")
        assert items == [] and total == 0


class TestSyncDegradation:
    def test_sync_returns_degraded_when_no_source(self, db_session: Session, monkeypatch: pytest.MonkeyPatch) -> None:
        """所有数据源失败时：不伪造新闻，返回空并标记降级。"""
        monkeypatch.setattr(news_service, "fetch_rss_news", lambda: [])
        monkeypatch.setattr(news_service, "_fetch_akshare_news", lambda codes: [])
        result = sync_news(db_session)
        assert result["degraded"] is True
        assert result["fetched"] == 0
        assert result["inserted"] == 0
        assert result["message"]
        assert db_session.query(NewsItem).count() == 0

    def test_sync_inserts_real_items(self, db_session: Session, monkeypatch: pytest.MonkeyPatch) -> None:
        _add_instrument(db_session)
        fake = [
            RawNews(source="rss", title="易方达消费行业股票基金公告", url="https://x/a"),
            RawNews(source="rss", title="大盘快讯", url="https://x/b"),
        ]
        monkeypatch.setattr(news_service, "fetch_rss_news", lambda: fake)
        monkeypatch.setattr(news_service, "_fetch_akshare_news", lambda codes: [])
        result = sync_news(db_session)
        assert result["degraded"] is False
        assert result["inserted"] == 2
        assert db_session.query(NewsItem).count() == 2


class TestRoutes:
    def test_news_route_registered(self, client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(news_service, "fetch_rss_news", lambda: [])
        monkeypatch.setattr(news_service, "_fetch_akshare_news", lambda codes: [])
        response = client.get("/api/news?scope=market")
        assert response.status_code == 200
        body = response.json()
        assert body["scope"] == "market"
        assert body["items"] == []
        assert body["total"] == 0

    def test_news_sync_route(self, client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
        fake = [RawNews(source="rss", title="接口同步新闻", url="https://x/sync")]
        monkeypatch.setattr(news_service, "fetch_rss_news", lambda: fake)
        monkeypatch.setattr(news_service, "_fetch_akshare_news", lambda codes: [])
        response = client.post("/api/news/sync")
        assert response.status_code == 200
        body = response.json()
        assert body["inserted"] == 1
        assert body["degraded"] is False

        # 同步后可通过 market 范围查到
        list_resp = client.get("/api/news?scope=market")
        assert list_resp.status_code == 200
        data = list_resp.json()
        assert data["total"] == 1
        assert data["items"][0]["title"] == "接口同步新闻"
        assert data["items"][0]["related_codes"] == []
        assert data["last_sync"]["inserted"] == 1

    def test_invalid_scope_rejected(self, client: TestClient) -> None:
        response = client.get("/api/news?scope=whatever")
        assert response.status_code == 422
