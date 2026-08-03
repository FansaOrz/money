"""基金新闻事件分析、映射与量化合成测试。"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models import (
    FundCatalogEntry,
    FundHolding,
    FundNewsImpact,
    Instrument,
    InstrumentType,
    NewsEvent,
    NewsEventItem,
    NewsItem,
)
from app.schemas.quant import FundAdvice
from app.services.fund_news_analysis import (
    analyze_pending_news,
    combine_fund_advice,
    get_fund_news_view,
)


def _instrument(db: Session, code: str, name: str) -> Instrument:
    item = Instrument(code=code, name=name, type=InstrumentType.FUND, currency="CNY")
    db.add(item)
    db.commit()
    return item


def _news(
    db: Session,
    *,
    content_hash: str,
    title: str,
    summary: str | None = None,
    source: str = "eastmoney_rss",
) -> NewsItem:
    item = NewsItem(
        source=source,
        title=title,
        summary=summary,
        url=f"https://example.com/{content_hash}",
        published_at=datetime.now(UTC) - timedelta(hours=1),
        content_hash=content_hash,
    )
    db.add(item)
    db.commit()
    return item


def _base_advice(*, score: int, action: str) -> FundAdvice:
    labels = {
        "add": "可以考虑加仓",
        "hold": "继续持有",
        "watch": "暂时观望",
        "reduce": "建议适当减仓",
        "reduce_more": "建议明显减仓",
    }
    return FundAdvice(
        action=action,
        label=labels[action],
        score=score,
        confidence="high",
        horizon="未来 1～3 个月",
        summary="量化基础结论",
        reasons=["历史数据理由"],
        risks=[],
        invalidation="趋势反转时失效",
    )


def test_cross_source_duplicates_merge_into_one_event(db_session: Session) -> None:
    _news(db_session, content_hash="a", title="美联储宣布降息，美股上涨")
    _news(
        db_session,
        content_hash="b",
        title="快讯：美联储宣布降息 美股上涨",
        source="sina_finance",
    )

    result = analyze_pending_news(db_session)

    assert result["events"] == 1
    event = db_session.query(NewsEvent).one()
    assert event.source_count == 2
    assert db_session.query(NewsEventItem).count() == 2


def test_market_event_maps_to_matching_fund_only(db_session: Session) -> None:
    spx = _instrument(db_session, "008401", "大成标普500等权重指数(QDII)C人民币")
    cn = _instrument(db_session, "110022", "易方达消费行业股票")
    db_session.add_all(
        [
            FundCatalogEntry(
                code=spx.code,
                name=spx.name,
                market="us_spx",
                active=True,
            ),
            FundCatalogEntry(
                code=cn.code,
                name=cn.name,
                market="cn",
                active=True,
            ),
        ]
    )
    db_session.commit()
    _news(db_session, content_hash="market", title="美股上涨，标普500创阶段新高")

    analyze_pending_news(db_session)

    spx_view = get_fund_news_view(db_session, spx.id)
    cn_view = get_fund_news_view(db_session, cn.id)
    assert spx_view["event_count"] == 1
    assert spx_view["score"] > 0
    assert cn_view["event_count"] == 0


def test_holding_news_is_diluted_by_disclosed_weight(db_session: Session) -> None:
    fund = _instrument(db_session, "000001", "测试消费基金")
    db_session.add(
        FundHolding(
            instrument_id=fund.id,
            report_date=datetime.now(UTC).date(),
            rank=1,
            stock_code="600519",
            stock_name="贵州茅台",
            weight=Decimal("5.0"),
            source="test",
        )
    )
    db_session.commit()
    _news(db_session, content_hash="holding", title="贵州茅台业绩预亏，市场下调预期")

    analyze_pending_news(db_session)

    impact = db_session.query(FundNewsImpact).one()
    assert impact.relation_type == "holding"
    assert impact.exposure_ratio == 0.05
    assert -5 < impact.signed_score < 0


def test_roundup_summary_does_not_link_many_funds(db_session: Session) -> None:
    _instrument(db_session, "000001", "测试消费基金")
    _instrument(db_session, "000002", "测试科技基金")
    _news(
        db_session,
        content_hash="roundup",
        title="国内四大证券报纸、重要财经媒体头版头条内容精华摘要",
        summary="测试消费基金、测试科技基金以及多家公司今日发布消息。",
    )

    analyze_pending_news(db_session)

    assert db_session.query(FundNewsImpact).count() == 0


def test_positive_news_cannot_turn_weak_trend_into_add(db_session: Session) -> None:
    fund = _instrument(db_session, "000003", "测试弱势基金")
    event = NewsEvent(
        canonical_key="event-positive",
        title="行业迎来利好",
        direction="positive",
        impact_level="high",
        impact_score=90,
        horizon_days=30,
        confidence=1,
        source_quality=1,
        plain_summary="行业消息偏正面。",
        facts_json="[]",
        targets_json="[]",
        source_count=2,
        analysis_method="llm",
        analyzed_at=datetime.now(UTC),
        latest_published_at=datetime.now(UTC),
        expires_at=datetime.now(UTC) + timedelta(days=30),
    )
    db_session.add(event)
    db_session.flush()
    db_session.add(
        FundNewsImpact(
            event_id=event.id,
            instrument_id=fund.id,
            relation_type="industry",
            relevance_score=1,
            exposure_ratio=1,
            signed_score=90,
            reason="测试行业全暴露",
        )
    )
    db_session.commit()

    advice, analysis = combine_fund_advice(
        db_session,
        fund,
        _base_advice(score=30, action="reduce"),
    )

    assert advice.action != "add"
    assert analysis["conflict_note"]


def test_verified_direct_high_risk_caps_add_signal(db_session: Session) -> None:
    fund = _instrument(db_session, "000004", "测试高风险基金")
    event = NewsEvent(
        canonical_key="event-risk",
        title="基金暂停赎回",
        direction="negative",
        impact_level="high",
        impact_score=90,
        horizon_days=30,
        confidence=1,
        source_quality=1,
        plain_summary="基金出现直接重大风险。",
        facts_json="[]",
        targets_json="[]",
        source_count=2,
        analysis_method="llm",
        analyzed_at=datetime.now(UTC),
        latest_published_at=datetime.now(UTC),
        expires_at=datetime.now(UTC) + timedelta(days=30),
    )
    db_session.add(event)
    db_session.flush()
    db_session.add(
        FundNewsImpact(
            event_id=event.id,
            instrument_id=fund.id,
            relation_type="direct_fund",
            relevance_score=1,
            exposure_ratio=1,
            signed_score=-90,
            reason="新闻直接提到该基金",
        )
    )
    db_session.commit()

    advice, analysis = combine_fund_advice(
        db_session,
        fund,
        _base_advice(score=85, action="add"),
    )

    assert advice.action == "watch"
    assert "重大负面事件" in (analysis["conflict_note"] or "")


def test_manual_analysis_route(client: TestClient, db_session: Session) -> None:
    _news(db_session, content_hash="route", title="A股市场上涨，政策释放利好")

    response = client.post("/api/news/analyze")

    assert response.status_code == 200
    body = response.json()
    assert body["enabled"] is True
    assert body["events"] == 1
