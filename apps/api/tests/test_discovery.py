"""基金发现（全市场目录 + 候选池）测试。

全部外部数据（akshare fund_name_em / fund_open_fund_daily_em）均以 mock 替代，
不访问真实网络。

覆盖：
- 目录同步幂等 upsert、market/family/share_class 派生字段；
- fund_open_fund_daily_em 可选刷新 active 状态（默认不误标不活跃）；
- 数据源返回空时保留已有目录；
- 目录列表过滤/分页与统计；
- 候选池构建：过滤、家族去重（主份额优先）、分层配额、max_size 钳制 500~1000；
- 建池不触发净值回填，仅按库内已有净值标记 nav_ready；
- 路由：catalog sync/list/stats、pools build/list/detail。
"""

from datetime import date, timedelta
from decimal import Decimal

import pandas as pd
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import CandidatePool, FundCatalogEntry, FundNav, Instrument
from app.services import candidate_pool as pool_service
from app.services import fund_catalog as catalog_service
from app.services.candidate_pool import (
    MIN_MAX_SIZE,
    MIN_NAV_SAMPLES,
    PoolBuildParams,
    dedupe_families,
)

# ---------------------------------------------------------------------------
# mock 数据
# ---------------------------------------------------------------------------

CATALOG_ROWS = [
    # (代码, 简称, 拼音缩写, 拼音全称, 类型)
    ("110022", "易方达消费行业股票", "YFDXFHYGP", "YIFANGDAXIAOFEIHANGYEGUPIAO", "股票型"),
    ("110022C", "易方达消费行业股票C", "YFDXFHYGPC", "YIFANGDAXIAOFEIHANGYEGUPIAOC", "股票型"),
    ("000300", "广发沪深300ETF联接A", "GFHS300ETFLJA", "GUANGFAHUSHEN300ETFLIANJIEA", "指数型"),
    ("161725", "招商中证白酒指数A", "ZSZZBJZSA", "ZHAOSHANGZHONGZHENGBAIJIUZHISHUA", "指数型"),
    ("161726", "招商中证白酒指数C", "ZSZZBJZSC", "ZHAOSHANGZHONGZHENGBAIJIUZHISHUC", "指数型"),
    ("513100", "国泰纳斯达克100ETF", "GTNSDK100ETF", "GUOTAINASIDAKE100ETF", "QDII"),
    ("007280", "摩根日本精选股票", "MGRBJXGP", "MOGENRIBENJINGXUANGUPIAO", "QDII"),
    ("000198", "天弘余额宝货币", "THYEBHB", "TIANHONGYUEBAOHUOBI", "货币型"),
    ("110003", "易方达上证50指数增强A", "YFDSZ50ZSZQA", "YIFANGDASHANGZHENG50ZHISHUZENGQIANGA", "指数型"),
    ("070001", "嘉实成长收益混合A", "JSCZSYHHA", "JIASHICHENGZHANGSHOUYIHUNHEA", "混合型"),
    ("000478", "建信中证500指数增强A", "JXZ500ZSZQA", "JIANXINZHONGZHENG500ZHISHUZENGQIANGA", "指数型"),
    ("110026", "易方达创业板ETF联接C", "YFDCYBTFLJC", "YIFANGDACHUANGYEBANETFLIANJIEC", "指数型"),
]


def _mock_fund_name_df(rows: list[tuple[str, str, str, str, str]] | None = None) -> pd.DataFrame:
    rows = CATALOG_ROWS if rows is None else rows
    return pd.DataFrame(
        [
            {
                "基金代码": code,
                "拼音缩写": abbr,
                "基金简称": name,
                "基金全称": name,
                "拼音全称": full,
                "基金类型": ftype,
            }
            for code, name, abbr, full, ftype in rows
        ]
    )


def _mock_daily_df(codes: list[str]) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "基金代码": codes,
            "单位净值": ["1.0"] * len(codes),
            "累计净值": ["1.0"] * len(codes),
            "日增长率": ["0.5"] * len(codes),
            "申购状态": ["开放申购"] * len(codes),
            "赎回状态": ["开放赎回"] * len(codes),
        }
    )


@pytest.fixture()
def mock_akshare(monkeypatch: pytest.MonkeyPatch):
    """替换 akshare 的 fund_name_em / fund_open_fund_daily_em。

    测试通过 ``mock_akshare.names_df`` / ``mock_akshare.daily_df`` 控制返回值。
    """

    class _FakeAk:
        names_df: pd.DataFrame | None = None
        daily_df: pd.DataFrame | None = None

        def fund_name_em(self) -> pd.DataFrame:
            assert self.names_df is not None
            return self.names_df

        def fund_open_fund_daily_em(self) -> pd.DataFrame:
            assert self.daily_df is not None
            return self.daily_df

    fake = _FakeAk()
    fake.names_df = _mock_fund_name_df()
    fake.daily_df = _mock_daily_df([row[0] for row in CATALOG_ROWS][:8])
    monkeypatch.setitem(__import__("sys").modules, "akshare", fake)
    return fake


def _seed_catalog(db: Session) -> None:
    """不经 akshare，直接落库一份目录（供建池/路由测试用）。"""
    for code, name, _abbr, _full, ftype in CATALOG_ROWS:
        family, share = catalog_service.split_family_share(name)
        db.add(
            FundCatalogEntry(
                code=code,
                name=name,
                pinyin_abbr=_abbr,
                fund_type=ftype,
                market=__import__("app.services.quant_factors", fromlist=["classify_market"]).classify_market(name),
                family=family,
                share_class=share,
                active=True,
            )
        )
    db.commit()


# ---------------------------------------------------------------------------
# split_family_share 纯函数
# ---------------------------------------------------------------------------


class TestSplitFamilyShare:
    def test_plain_fund(self) -> None:
        family, share = catalog_service.split_family_share("易方达消费行业股票")
        assert family == "易方达消费行业股票"
        assert share is None

    def test_share_suffix(self) -> None:
        family, share = catalog_service.split_family_share("易方达消费行业股票C")
        assert family == "易方达消费行业股票"
        assert share == "C"

    def test_etf_not_split(self) -> None:
        family, share = catalog_service.split_family_share("国泰纳斯达克100ETF")
        assert family == "国泰纳斯达克100ETF"
        assert share is None


# ---------------------------------------------------------------------------
# 目录同步
# ---------------------------------------------------------------------------


class TestSyncFundCatalog:
    def test_sync_inserts_all(self, db_session: Session, mock_akshare) -> None:
        result = catalog_service.sync_fund_catalog(db_session)
        assert result["inserted"] == len(CATALOG_ROWS)
        assert result["updated"] == 0
        assert result["catalog_size"] == len(CATALOG_ROWS)
        entry = db_session.scalar(
            select(FundCatalogEntry).where(FundCatalogEntry.code == "513100")
        )
        assert entry is not None
        assert entry.market == "us_nasdaq"
        assert entry.fund_type == "QDII"
        assert entry.active is True

    def test_sync_idempotent(self, db_session: Session, mock_akshare) -> None:
        catalog_service.sync_fund_catalog(db_session)
        result = catalog_service.sync_fund_catalog(db_session)
        assert result["inserted"] == 0
        assert result["updated"] == len(CATALOG_ROWS)
        total = db_session.scalar(select(FundCatalogEntry).order_by(FundCatalogEntry.id))
        assert total is not None
        count = db_session.query(FundCatalogEntry).count()
        assert count == len(CATALOG_ROWS)

    def test_sync_empty_source_keeps_existing(
        self, db_session: Session, mock_akshare
    ) -> None:
        catalog_service.sync_fund_catalog(db_session)
        mock_akshare.names_df = pd.DataFrame()
        with pytest.raises(RuntimeError, match="空数据"):
            catalog_service.sync_fund_catalog(db_session)
        assert db_session.query(FundCatalogEntry).count() == len(CATALOG_ROWS)

    def test_refresh_active_marks_only_present(
        self, db_session: Session, mock_akshare
    ) -> None:
        catalog_service.sync_fund_catalog(db_session)
        active_codes = [row[0] for row in CATALOG_ROWS][:5]
        mock_akshare.daily_df = _mock_daily_df(active_codes)
        result = catalog_service.sync_fund_catalog(db_session, refresh_active=True)
        assert result["active_marked"] == 0  # 全部默认 active，无需改回
        assert result["inactive_marked"] == 0  # mark_inactive 默认关闭，不误伤
        assert db_session.query(FundCatalogEntry).filter_by(active=True).count() == len(
            CATALOG_ROWS
        )

    def test_refresh_active_with_mark_inactive(
        self, db_session: Session, mock_akshare
    ) -> None:
        catalog_service.sync_fund_catalog(db_session)
        active_codes = [row[0] for row in CATALOG_ROWS][:5]
        mock_akshare.daily_df = _mock_daily_df(active_codes)
        result = catalog_service.sync_fund_catalog(
            db_session, refresh_active=True, mark_inactive=True
        )
        assert result["inactive_marked"] == len(CATALOG_ROWS) - len(active_codes)
        assert db_session.query(FundCatalogEntry).filter_by(active=True).count() == len(
            active_codes
        )

    def test_refresh_active_empty_daily_skips(
        self, db_session: Session, mock_akshare
    ) -> None:
        catalog_service.sync_fund_catalog(db_session)
        mock_akshare.daily_df = pd.DataFrame()
        result = catalog_service.sync_fund_catalog(
            db_session, refresh_active=True, mark_inactive=True
        )
        assert result["inactive_marked"] == 0
        assert db_session.query(FundCatalogEntry).filter_by(active=True).count() == len(
            CATALOG_ROWS
        )

    def test_derived_family_market(self, db_session: Session, mock_akshare) -> None:
        catalog_service.sync_fund_catalog(db_session)
        entry_c = db_session.scalar(
            select(FundCatalogEntry).where(FundCatalogEntry.code == "161726")
        )
        assert entry_c is not None
        assert entry_c.family == "招商中证白酒指数"
        assert entry_c.share_class == "C"
        entry_a = db_session.scalar(
            select(FundCatalogEntry).where(FundCatalogEntry.code == "161725")
        )
        assert entry_a is not None
        assert entry_a.family == "招商中证白酒指数"
        assert entry_a.share_class == "A"


# ---------------------------------------------------------------------------
# 家族去重 / 分层配额（纯函数）
# ---------------------------------------------------------------------------


class TestDedupeFamilies:
    def test_keeps_primary_share(self, db_session: Session) -> None:
        entries = [
            FundCatalogEntry(
                code="110022C", name="易方达消费行业股票C",
                family="易方达消费行业股票", share_class="C",
            ),
            FundCatalogEntry(
                code="110022", name="易方达消费行业股票",
                family="易方达消费行业股票", share_class=None,
            ),
        ]
        deduped, removed = dedupe_families(entries)
        assert removed == 1
        assert [e.code for e in deduped] == ["110022"]

    def test_prefers_a_over_c(self, db_session: Session) -> None:
        entries = [
            FundCatalogEntry(
                code="161726", name="招商中证白酒指数C",
                family="招商中证白酒指数", share_class="C",
            ),
            FundCatalogEntry(
                code="161725", name="招商中证白酒指数A",
                family="招商中证白酒指数", share_class="A",
            ),
        ]
        deduped, removed = dedupe_families(entries)
        assert removed == 1
        assert [e.code for e in deduped] == ["161725"]


class TestTierQuotas:
    def _make_entries(self, n_t1: int, n_t2: int, n_t3: int) -> list[FundCatalogEntry]:
        entries: list[FundCatalogEntry] = []
        for idx in range(n_t1):
            entries.append(
                FundCatalogEntry(
                    code=f"T1{idx:04d}", name=f"消费股票{idx}", fund_type="股票型", market="cn"
                )
            )
        for idx in range(n_t2):
            entries.append(
                FundCatalogEntry(
                    code=f"T2{idx:04d}",
                    name=f"纳斯达克{idx}",
                    fund_type="QDII",
                    market="us_nasdaq",
                )
            )
        for idx in range(n_t3):
            entries.append(
                FundCatalogEntry(
                    code=f"T3{idx:04d}", name=f"纯债债券{idx}", fund_type="债券型", market="bond"
                )
            )
        return entries

    def test_quota_distribution(self) -> None:
        entries = self._make_entries(1000, 500, 200)
        picked, counts = pool_service.apply_tier_quotas(entries, 800)
        assert len(picked) == 800
        assert counts["tier1"] == 560  # 70%
        assert counts["tier2"] == 200  # 25%
        assert counts["tier3"] == 40  # 5%

    def test_overflow_backfill(self) -> None:
        # tier2/tier3 不足配额时，额度顺延给 tier1（tier1 充足可补齐到 800）
        entries = self._make_entries(1000, 50, 10)
        picked, counts = pool_service.apply_tier_quotas(entries, 800)
        assert len(picked) == 800
        assert counts["tier2"] == 50
        assert counts["tier3"] == 10
        assert counts["tier1"] == 740  # 560 配额 + 190 顺延

    def test_total_shortage_keeps_all(self) -> None:
        # 全部候选不足 max_size 时，有多少取多少
        entries = self._make_entries(100, 20, 5)
        picked, counts = pool_service.apply_tier_quotas(entries, 800)
        assert len(picked) == 125
        assert counts == {"tier1": 100, "tier2": 20, "tier3": 5}


# ---------------------------------------------------------------------------
# 建池（服务层）
# ---------------------------------------------------------------------------


class TestBuildCandidatePool:
    def test_build_filters_dedupes_and_marks_nav(
        self, db_session: Session, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _seed_catalog(db_session)
        # 放宽规模钳制，便于小样本断言
        monkeypatch.setattr(pool_service, "MIN_MAX_SIZE", 1)
        monkeypatch.setattr(PoolBuildParams, "clamped_max_size", lambda self: self.max_size)

        # 一只基金已有足够净值 → nav_ready=True；建池不应触发回填
        instrument = Instrument(code="110022", name="易方达消费行业股票")
        db_session.add(instrument)
        db_session.commit()
        start = date.today() - timedelta(days=MIN_NAV_SAMPLES + 10)
        for offset in range(MIN_NAV_SAMPLES + 1):
            db_session.add(
                FundNav(
                    instrument_id=instrument.id,
                    nav_date=start + timedelta(days=offset),
                    unit_nav=Decimal("1.0"),
                    source="test",
                )
            )
        db_session.commit()

        pool = pool_service.build_candidate_pool(db_session, PoolBuildParams(max_size=10))
        codes = [m.code for m in pool.members]
        # 家族去重：110022C 被剔除；联接基金被关键词剔除
        assert "110022C" not in codes
        assert "000300" not in codes  # 联接
        assert "110026" not in codes  # 联接
        # 主基金入选且 nav_ready
        member = next(m for m in pool.members if m.code == "110022")
        assert member.nav_ready is True
        assert member.nav_samples >= MIN_NAV_SAMPLES
        # 未回填净值的成员 nav_ready=False，但仍入池（不阻塞建池）
        other = next(m for m in pool.members if m.code == "161725")
        assert other.nav_ready is False
        assert pool.member_count == len(codes)
        assert pool.status == "ready"

    def test_max_size_clamped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        assert PoolBuildParams(max_size=10).clamped_max_size() == MIN_MAX_SIZE
        assert PoolBuildParams(max_size=5000).clamped_max_size() == 1000
        assert PoolBuildParams(max_size=800).clamped_max_size() == 800

    def test_inactive_excluded(
        self, db_session: Session, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _seed_catalog(db_session)
        entry = db_session.scalar(
            select(FundCatalogEntry).where(FundCatalogEntry.code == "110022")
        )
        assert entry is not None
        entry.active = False
        db_session.commit()
        monkeypatch.setattr(PoolBuildParams, "clamped_max_size", lambda self: self.max_size)
        pool = pool_service.build_candidate_pool(db_session, PoolBuildParams(max_size=50))
        codes = [m.code for m in pool.members]
        # 110022 不活跃被剔除；其 C 份额仍在但同家族代表份额逻辑不受影响
        assert "110022" not in codes


# ---------------------------------------------------------------------------
# 路由
# ---------------------------------------------------------------------------


class TestDiscoveryRoutes:
    def test_catalog_sync_route(
        self, client: TestClient, db_session: Session, mock_akshare
    ) -> None:
        response = client.post("/api/discovery/catalog/sync", json={})
        assert response.status_code == 200
        body = response.json()
        assert body["inserted"] == len(CATALOG_ROWS)
        assert body["catalog_size"] == len(CATALOG_ROWS)
        # 幂等：再次同步不再新增
        again = client.post("/api/discovery/catalog/sync", json={}).json()
        assert again["inserted"] == 0

    def test_catalog_sync_route_source_failure(
        self, client: TestClient, mock_akshare
    ) -> None:
        mock_akshare.names_df = pd.DataFrame()
        response = client.post("/api/discovery/catalog/sync", json={})
        assert response.status_code == 502

    def test_catalog_list_and_stats(
        self, client: TestClient, db_session: Session
    ) -> None:
        _seed_catalog(db_session)
        response = client.get("/api/discovery/catalog", params={"fund_type": "QDII"})
        assert response.status_code == 200
        body = response.json()
        assert body["total"] == 2
        assert {item["code"] for item in body["items"]} == {"513100", "007280"}

        keyword = client.get("/api/discovery/catalog", params={"keyword": "纳斯达克"}).json()
        assert keyword["total"] == 1
        assert keyword["items"][0]["code"] == "513100"

        stats = client.get("/api/discovery/catalog/stats").json()
        assert stats["total"] == len(CATALOG_ROWS)
        assert stats["active"] == len(CATALOG_ROWS)
        assert stats["by_type"]["股票型"] == 2
        assert "us_nasdaq" in stats["by_market"]

    def test_pool_build_list_detail(
        self, client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _seed_catalog(db_session)
        monkeypatch.setattr(PoolBuildParams, "clamped_max_size", lambda self: self.max_size)
        response = client.post("/api/discovery/pools/build", json={"max_size": 10})
        assert response.status_code == 201
        body = response.json()
        assert body["member_count"] <= 10
        assert body["summary"]["tier_counts"]
        assert len(body["members"]) == body["member_count"]
        pool_id = body["id"]

        listed = client.get("/api/discovery/pools").json()
        assert listed["total"] >= 1
        assert listed["items"][0]["id"] == pool_id

        detail = client.get(f"/api/discovery/pools/{pool_id}").json()
        assert detail["id"] == pool_id
        assert detail["params"]["max_size"] == 10

        missing = client.get("/api/discovery/pools/99999")
        assert missing.status_code == 404

    def test_pool_refresh_nav(
        self, client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _seed_catalog(db_session)
        monkeypatch.setattr(PoolBuildParams, "clamped_max_size", lambda self: self.max_size)
        pool_id = client.post(
            "/api/discovery/pools/build", json={"max_size": 10}
        ).json()["id"]
        # 建池时无净值，全部 nav_ready=False
        detail = client.get(f"/api/discovery/pools/{pool_id}").json()
        assert all(not m["nav_ready"] for m in detail["members"])

        # 模拟后续回填任务写入净值
        pool = db_session.get(CandidatePool, pool_id)
        assert pool is not None
        member_codes = [m.code for m in pool.members]
        instrument = db_session.scalar(
            select(Instrument).where(Instrument.code == member_codes[0])
        )
        if instrument is None:
            instrument = Instrument(code=member_codes[0], name=pool.members[0].name)
            db_session.add(instrument)
            db_session.commit()
        start = date.today() - timedelta(days=MIN_NAV_SAMPLES + 5)
        for offset in range(MIN_NAV_SAMPLES + 1):
            db_session.add(
                FundNav(
                    instrument_id=instrument.id,
                    nav_date=start + timedelta(days=offset),
                    unit_nav=Decimal("1.0"),
                    source="test",
                )
            )
        db_session.commit()

        refreshed = client.post(f"/api/discovery/pools/{pool_id}/refresh-nav").json()
        first = next(m for m in refreshed["members"] if m["code"] == member_codes[0])
        assert first["nav_ready"] is True
        assert refreshed["summary"]["nav_ready_count"] == 1
