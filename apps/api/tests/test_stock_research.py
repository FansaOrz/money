"""A 股研究数据层测试。

全部外部数据调用通过 monkeypatch app.services.research.ak_fetch 替换为本地
DataFrame，测试全程离线；另覆盖网络失败（返回 None）时的优雅降级与幂等性。
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pandas as pd
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    IndexConstituent,
    IndexMembershipEvent,
    StockDailyBar,
    StockFinancialIndicator,
    StockIndustry,
    StockMaster,
    StockNameHistory,
    StockReportDisclosure,
    StockSyncState,
    StockUniverseSnapshot,
    StockValuation,
)
from app.services.research import ak_fetch, parquet_store, stock_data, stock_fundamentals, stock_universe


# ---------------------------------------------------------------------------
# mock 数据
# ---------------------------------------------------------------------------

def _master_frame() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {"code": "600519", "name": "贵州茅台"},
            {"code": "000001", "name": "平安银行"},
            {"code": "300750", "name": "宁德时代"},
            {"code": "920001", "name": "北交测试"},
        ]
    )


def _daily_frame() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "date": "2024-01-02", "open": 10.0, "high": 10.5, "low": 9.8,
                "close": 10.2, "volume": 1000, "amount": 10200.0,
                "outstanding_share": 1e8, "turnover": 0.5,
            },
            {
                "date": "2024-01-03", "open": 10.2, "high": 10.6, "low": 10.1,
                "close": 10.4, "volume": 2000, "amount": 20800.0,
                "outstanding_share": 1e8, "turnover": 1.0,
            },
        ]
    )


def _cons_frame() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {"成分券代码": "600519", "成分券名称": "贵州茅台", "纳入日期": "2020-06-15"},
            {"成分券代码": "000001", "成分券名称": "平安银行", "纳入日期": "2019-12-16"},
        ]
    )


def _financial_frame() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {"日期": "2023-12-31", "摊薄每股收益(元)": 5.2, "净资产收益率(%)": 30.1},
            {"日期": "2024-03-31", "摊薄每股收益(元)": 1.3, "净资产收益率(%)": 7.5},
        ]
    )


def _disclosure_frame(market: str) -> pd.DataFrame:
    """模拟当前 akshare 全市场披露快照：一次请求返回该市场分区全部股票。"""
    rows = [
        {"股票代码": "600519", "股票简称": "贵州茅台", "首次预约": "2024-04-20",
         "初次变更": None, "二次变更": None, "三次变更": None, "实际披露": "2024-04-26"},
        {"股票代码": "000001", "股票简称": "平安银行", "首次预约": "2024-04-19",
         "初次变更": None, "二次变更": None, "三次变更": None, "实际披露": "2024-04-20"},
    ]
    if market == "北交所":
        rows = [
            {"股票代码": "920001", "股票简称": "北交测试", "首次预约": "2024-04-25",
             "初次变更": None, "二次变更": None, "三次变更": None, "实际披露": "2024-04-28"},
        ]
    elif market == "深市":
        rows = rows[1:]
    elif market == "沪市":
        rows = rows[:1]
    return pd.DataFrame(rows)


def _valuation_frame() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {"date": "2024-01-02", "value": 21500.0},
            {"date": "2024-01-03", "value": 21800.0},
        ]
    )


def _name_hist_frame() -> pd.DataFrame:
    """模拟 ak.stock_info_change_name：行序旧->新，名称列含区间标记。"""
    return pd.DataFrame(
        [
            {"index": 1, "name": "某某股份"},
            {"index": 2, "name": "ST某某->某某股份"},
        ]
    )


def _industry_boards_frame() -> pd.DataFrame:
    return pd.DataFrame([{"板块名称": "酿酒行业"}, {"板块名称": "银行"}])


def _industry_cons_frame(board: str) -> pd.DataFrame:
    rows = {
        "酿酒行业": [{"代码": "600519", "名称": "贵州茅台"}],
        "银行": [{"代码": "000001", "名称": "平安银行"}],
    }
    return pd.DataFrame(rows.get(board, []))


@pytest.fixture()
def mock_ak(monkeypatch: pytest.MonkeyPatch) -> None:
    """替换所有外部抓取为本地 DataFrame。"""
    monkeypatch.setattr(ak_fetch, "fetch_stock_code_name", lambda: _master_frame())
    monkeypatch.setattr(
        ak_fetch, "fetch_stock_daily_sina", lambda symbol, adjust="": _daily_frame()
    )
    monkeypatch.setattr(
        ak_fetch,
        "fetch_stock_daily_eastmoney",
        lambda symbol, **kwargs: None,
    )
    monkeypatch.setattr(
        ak_fetch,
        "fetch_stock_daily_tencent",
        lambda symbol, **kwargs: None,
    )
    monkeypatch.setattr(ak_fetch, "fetch_index_cons", lambda symbol: _cons_frame())
    monkeypatch.setattr(
        ak_fetch, "fetch_financial_indicator_eastmoney", lambda symbol: None
    )
    monkeypatch.setattr(ak_fetch, "fetch_financial_indicator", lambda symbol: _financial_frame())
    monkeypatch.setattr(
        ak_fetch, "fetch_financial_indicator_ths", lambda symbol: None
    )
    monkeypatch.setattr(
        ak_fetch, "fetch_report_disclosure", lambda market, period: _disclosure_frame(market)
    )
    monkeypatch.setattr(
        ak_fetch, "fetch_valuation_baidu", lambda symbol, indicator, period="近一年": _valuation_frame()
    )
    monkeypatch.setattr(ak_fetch, "fetch_name_change_hist", lambda symbol: _name_hist_frame())
    monkeypatch.setattr(ak_fetch, "fetch_industry_boards", lambda: _industry_boards_frame())
    monkeypatch.setattr(
        ak_fetch, "fetch_industry_cons", lambda symbol: _industry_cons_frame(symbol)
    )
    monkeypatch.setattr(ak_fetch, "fetch_industry_change_cninfo", lambda symbol: None)
    monkeypatch.setattr(ak_fetch, "fetch_stock_profile_cninfo", lambda symbol: None)


@pytest.fixture(autouse=True)
def research_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """把数据湖根目录指向测试临时目录（模块内全部测试隔离，不污染真实 ./data）。"""
    root = tmp_path / "research"
    original_data_root = parquet_store.data_root

    def _patched(r: Path | None = None) -> Path:
        return original_data_root(root)

    monkeypatch.setattr(parquet_store, "data_root", _patched)
    return root


# ---------------------------------------------------------------------------
# master
# ---------------------------------------------------------------------------

def test_sync_master(db_session: Session, mock_ak: None) -> None:
    result = stock_data.sync_stock_master(db_session)
    assert result["status"] == "success"
    assert result["total"] == 4
    rows = db_session.scalars(select(StockMaster).order_by(StockMaster.code)).all()
    assert [r.code for r in rows] == ["000001", "300750", "600519", "920001"]
    assert rows[2].exchange == "sh"
    # 北交所 92 新号段识别
    assert rows[3].exchange == "bj"
    assert stock_data.sina_symbol("920001") == "bj920001"

    # 幂等：再次同步不产生重复
    result2 = stock_data.sync_stock_master(db_session)
    assert result2["total"] == 4
    assert db_session.scalar(select(StockMaster).where(StockMaster.code == "600519")) is not None


def test_to_date_rejects_nat_and_nan() -> None:
    assert stock_data._to_date(pd.NaT) is None
    assert stock_data._to_date(float("nan")) is None
    assert stock_data._to_date("NaT") is None


def test_sync_master_network_failure(db_session: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ak_fetch, "fetch_stock_code_name", lambda: None)
    result = stock_data.sync_stock_master(db_session)
    assert result["status"] == "failed"
    assert result["errors"]
    assert db_session.scalar(select(StockMaster).limit(1)) is None


# ---------------------------------------------------------------------------
# 日线
# ---------------------------------------------------------------------------

def test_sync_daily_and_read_back(db_session: Session, mock_ak: None, research_root: Path) -> None:
    stock_data.sync_stock_master(db_session)
    result = stock_data.sync_stock_daily(db_session, ["600519"], fetch_qfq=True)
    assert result["status"] == "success"
    assert result["updated"] == 1

    bar = db_session.get(StockDailyBar, "600519")
    assert bar is not None
    assert bar.rows == 2
    assert bar.first_trade_date == date(2024, 1, 2)
    assert bar.last_trade_date == date(2024, 1, 3)
    assert bar.available_at is not None

    rows = stock_data.get_daily_bars(db_session, "600519")
    assert len(rows) == 2
    assert rows[0]["close"] == pytest.approx(10.2)

    qfq_rows = stock_data.get_daily_bars(db_session, "600519", layer=parquet_store.DAILY_QFQ)
    assert len(qfq_rows) == 2


def test_sync_daily_incremental_resume(db_session: Session, mock_ak: None, research_root: Path) -> None:
    """断点续传：追加新日期后按 trade_date 去重合并。"""
    stock_data.sync_stock_master(db_session)
    stock_data.sync_stock_daily(db_session, ["600519"], fetch_qfq=False)

    appended = pd.DataFrame(
        [
            {"date": "2024-01-03", "open": 10.2, "high": 10.6, "low": 10.1, "close": 99.0,
             "volume": 1, "amount": 1.0, "outstanding_share": 1e8, "turnover": 0.1},
            {"date": "2024-01-04", "open": 10.4, "high": 10.8, "low": 10.3, "close": 10.7,
             "volume": 3000, "amount": 32100.0, "outstanding_share": 1e8, "turnover": 1.5},
        ]
    )
    # 直接调用内部写路径，模拟“新一批数据覆盖旧行 + 新增一行”
    stock_data.parquet_store.write_daily(
        "600519", stock_data.parse_sina_daily_frame("600519", appended), incremental=True
    )
    rows = stock_data.get_daily_bars(db_session, "600519")
    assert [r["trade_date"] for r in rows] == [date(2024, 1, 2), date(2024, 1, 3), date(2024, 1, 4)]
    # 同日以新行（close=99.0）为准
    assert rows[1]["close"] == pytest.approx(99.0)


def test_sync_daily_network_failure(
    db_session: Session, monkeypatch: pytest.MonkeyPatch, research_root: Path
) -> None:
    monkeypatch.setattr(ak_fetch, "fetch_stock_code_name", lambda: _master_frame())
    stock_data.sync_stock_master(db_session)
    monkeypatch.setattr(ak_fetch, "fetch_stock_daily_sina", lambda symbol, adjust="": None)
    monkeypatch.setattr(
        ak_fetch, "fetch_stock_daily_eastmoney", lambda symbol, **kwargs: None
    )
    monkeypatch.setattr(
        ak_fetch, "fetch_stock_daily_tencent", lambda symbol, **kwargs: None
    )
    result = stock_data.sync_stock_daily(db_session, ["600519", "000001"])
    assert result["failed"] == 2
    assert result["updated"] == 0
    assert result["status"] == "failed"
    assert len(result["errors"]) == 2
    bar = db_session.get(StockDailyBar, "600519")
    assert bar is not None and bar.last_error is not None
    # 失败不产生任何 Parquet 文件
    assert stock_data.get_daily_bars(db_session, "600519") == []


# ---------------------------------------------------------------------------
# 日线断点续传 / partial
# ---------------------------------------------------------------------------

def test_sync_daily_auto_batch_never_synced_first(
    db_session: Session, mock_ak: None, research_root: Path
) -> None:
    """自动选批：未同步优先，批次大小生效，不再每次从头取前 N 只。"""
    stock_data.sync_stock_master(db_session)
    result = stock_data.sync_stock_daily(db_session, limit=2, fetch_qfq=False)
    assert result["status"] == "success"
    assert result["total"] == 2  # master 有 4 只，只取一批 2 只
    tracked = db_session.scalars(select(StockDailyBar.code)).all()
    assert len(tracked) == 2

    # 第二批：继续取剩余未同步的，不重复已同步的
    result2 = stock_data.sync_stock_daily(db_session, limit=2, fetch_qfq=False)
    assert result2["total"] == 2
    tracked2 = set(db_session.scalars(select(StockDailyBar.code)).all())
    assert len(tracked2) == 4


def test_sync_daily_error_retry_and_stale_order(
    db_session: Session, monkeypatch: pytest.MonkeyPatch, research_root: Path
) -> None:
    """有错误的股票优先重试，其余按最久未更新排序。"""
    monkeypatch.setattr(ak_fetch, "fetch_stock_code_name", lambda: _master_frame())
    stock_data.sync_stock_master(db_session)
    monkeypatch.setattr(ak_fetch, "fetch_stock_daily_sina", lambda symbol, adjust="": _daily_frame())
    stock_data.sync_stock_daily(db_session, ["000001", "300750"], fetch_qfq=False)

    # 人为制造一只错误股票与陈旧断点
    from datetime import datetime as dt
    err_bar = StockDailyBar(code="600519", last_error="网络超时")
    stale_bar = StockDailyBar(
        code="920001", available_at=dt(2020, 1, 1), first_trade_date=date(2020, 1, 2)
    )
    db_session.add_all([err_bar, stale_bar])
    db_session.commit()

    batch = stock_data._select_daily_batch(db_session, 3)
    # 错误重试优先于普通陈旧（available_at 最旧但无错误）；limit 截断后新成功股靠后
    assert batch.index("600519") < batch.index("920001")
    assert "000001" in batch  # 成功股也会按最久未更新轮换入批

    # 全量额度下顺序：错误(600519) -> 陈旧(920001, 2020) -> 新近成功(000001/300750)
    full = stock_data._select_daily_batch(db_session, 4)
    assert full.index("600519") < full.index("920001") < full.index("000001")


def test_sync_daily_resume_after_failed_run(
    db_session: Session, monkeypatch: pytest.MonkeyPatch, research_root: Path
) -> None:
    """上次带错误结束 -> 从 last_code 游标继续，而不是回到表头。"""
    monkeypatch.setattr(ak_fetch, "fetch_stock_code_name", lambda: _master_frame())
    stock_data.sync_stock_master(db_session)
    calls: list[str] = []

    def flaky(symbol: str, adjust: str = "") -> pd.DataFrame | None:
        calls.append(symbol)
        return None if symbol == "sz000001" else _daily_frame()

    monkeypatch.setattr(ak_fetch, "fetch_stock_daily_sina", flaky)
    monkeypatch.setattr(
        ak_fetch, "fetch_stock_daily_eastmoney", lambda symbol, **kwargs: None
    )
    monkeypatch.setattr(
        ak_fetch, "fetch_stock_daily_tencent", lambda symbol, **kwargs: None
    )
    result = stock_data.sync_stock_daily(db_session, ["000001", "300750"], fetch_qfq=False)
    assert result["status"] == "partial"  # 有成功有失败绝不能是 success
    assert result["last_code"] == "300750"
    state = db_session.get(StockSyncState, "daily")
    assert state.status == "partial"
    assert state.failed == 1 and state.updated == 1
    assert "000001" in (state.detail or "")  # 失败摘要已保存

    # 修复数据源，续跑：从 last_code 之后继续（跳过已干净同步的 300750）
    def healed(symbol: str, adjust: str = "") -> pd.DataFrame:
        calls.append(symbol)
        return _daily_frame()

    monkeypatch.setattr(ak_fetch, "fetch_stock_daily_sina", healed)
    calls.clear()
    result2 = stock_data.sync_stock_daily(db_session, limit=2, fetch_qfq=False)
    assert result2["status"] == "success"
    synced = {s[2:] for s in calls}
    assert "600519" in synced or "920001" in synced
    assert "000001" not in synced  # 不回头重抓已处理段


def test_sync_daily_progress_written_per_stock(
    db_session: Session, mock_ak: None, research_root: Path
) -> None:
    """每只股票处理后进度落 stock_sync_state（total/updated/failed/last_code）。"""
    stock_data.sync_stock_master(db_session)
    stock_data.sync_stock_daily(db_session, ["600519", "000001", "300750"], fetch_qfq=False)
    state = db_session.get(StockSyncState, "daily")
    assert state.total == 3
    assert state.updated == 3
    assert state.failed == 0
    assert state.last_code is not None  # 显式 codes 批次保留游标
    assert state.finished_at is not None


def test_final_status_rules() -> None:
    assert stock_data._final_status(1, 1) == "partial"
    assert stock_data._final_status(0, 2) == "failed"
    assert stock_data._final_status(2, 0) == "success"
    assert stock_data._final_status(0, 0, processed=0) == "partial"


# ---------------------------------------------------------------------------
# universe
# ---------------------------------------------------------------------------

def test_sync_index_cons(db_session: Session, mock_ak: None) -> None:
    result = stock_universe.sync_index_cons(db_session, ["000300"])
    assert result["status"] == "success"
    rows = db_session.scalars(select(IndexConstituent)).all()
    assert len(rows) == 2
    assert {r.stock_code for r in rows} == {"600519", "000001"}

    # 全量替换语义：第二次同步只保留最新成分
    result2 = stock_universe.sync_index_cons(db_session, ["000300"])
    assert result2["updated"] == 1
    assert len(db_session.scalars(select(IndexConstituent)).all()) == 2


def test_import_membership_events_and_replay(db_session: Session, mock_ak: None) -> None:
    stock_universe.sync_index_cons(db_session, ["000300"])
    csv_text = (
        "index_code,stock_code,stock_name,event_type,effective_date\n"
        "000300,600519,贵州茅台,add,2020-06-15\n"
        "000300,000001,平安银行,add,2019-12-16\n"
        "000300,000001,平安银行,remove,2023-06-12\n"
        "000300,300750,宁德时代,add,2023-06-12\n"
    )
    result = stock_universe.import_membership_events_csv(db_session, csv_text)
    assert result["imported"] == 4
    # 幂等：重复导入全部 skipped
    result2 = stock_universe.import_membership_events_csv(db_session, csv_text)
    assert result2["imported"] == 0 and result2["skipped"] == 4

    # 回放：2022 年末只有 600519 + 000001
    members_2022 = stock_universe.replay_membership(db_session, "000300", date(2022, 12, 31))
    assert set(members_2022) == {"600519", "000001"}
    # 回放：2023 年中调样后 000001 出、300750 进
    members_2023 = stock_universe.replay_membership(db_session, "000300", date(2023, 6, 30))
    assert set(members_2023) == {"600519", "300750"}

    # 物化快照 + get_universe 三种 basis
    stock_universe.materialize_snapshot(db_session, "000300", date(2023, 6, 30))
    snap = db_session.scalars(select(StockUniverseSnapshot)).all()
    assert len(snap) == 2

    current = stock_universe.get_universe(db_session, "000300")
    assert current["basis"] == "current"
    snap_basis = stock_universe.get_universe(db_session, "000300", date(2023, 6, 30))
    assert snap_basis["basis"] == "snapshot"
    assert {m["stock_code"] for m in snap_basis["members"]} == {"600519", "300750"}
    replay_basis = stock_universe.get_universe(db_session, "000300", date(2022, 12, 31))
    assert replay_basis["basis"] == "replay"
    assert {m["stock_code"] for m in replay_basis["members"]} == {"600519", "000001"}


def test_import_membership_events_bad_csv(db_session: Session) -> None:
    result = stock_universe.import_membership_events_csv(db_session, "a,b,c\n1,2,3\n")
    assert result["status"] == "failed"
    assert result["errors"]


def test_import_stocktoday_index_weights(db_session: Session, tmp_path: Path) -> None:
    snapshot_root = tmp_path / "tushare_snapshot"
    partition = snapshot_root / "indices" / "index_weight" / "000300.SH"
    partition.mkdir(parents=True)
    pd.DataFrame(
        [
            {
                "index_code": "000300.SH",
                "con_code": "000001.SZ",
                "trade_date": "20240131",
                "weight": 1.2,
            },
            {
                "index_code": "000300.SH",
                "con_code": "600519.SH",
                "trade_date": "20240131",
                "weight": 3.5,
            },
            {
                "index_code": "000300.SH",
                "con_code": "600519.SH",
                "trade_date": "20240229",
                "weight": 3.4,
            },
            {
                "index_code": "000300.SH",
                "con_code": "300750.SZ",
                "trade_date": "20240229",
                "weight": 2.1,
            },
        ]
    ).to_parquet(partition / "2024.parquet", index=False)

    result = stock_universe.import_stocktoday_index_weights(db_session, snapshot_root)
    assert result["status"] == "success"
    assert result["snapshots_imported"] == 4
    assert result["events_imported"] == 4

    snapshots = db_session.scalars(select(StockUniverseSnapshot)).all()
    assert len(snapshots) == 4
    events = db_session.scalars(select(IndexMembershipEvent)).all()
    assert len(events) == 4
    assert {event.source for event in events} == {"stocktoday:index_weight"}

    january = stock_universe.get_universe(
        db_session, "000300", date(2024, 1, 31)
    )
    assert january["basis"] == "snapshot"
    assert {row["stock_code"] for row in january["members"]} == {
        "000001",
        "600519",
    }
    february = stock_universe.get_universe(
        db_session, "000300", date(2024, 2, 29)
    )
    assert {row["stock_code"] for row in february["members"]} == {
        "300750",
        "600519",
    }

    repeated = stock_universe.import_stocktoday_index_weights(
        db_session, snapshot_root
    )
    assert repeated["snapshots_imported"] == 0
    assert repeated["events_imported"] == 0


def test_sync_index_cons_network_failure(db_session: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ak_fetch, "fetch_index_cons", lambda symbol: None)
    result = stock_universe.sync_index_cons(db_session, ["000300"])
    assert result["status"] == "failed"
    assert result["errors"]


# ---------------------------------------------------------------------------
# fundamentals
# ---------------------------------------------------------------------------

def test_sync_financial_and_available_at(db_session: Session, mock_ak: None) -> None:
    result = stock_fundamentals.sync_financial_indicators(db_session, ["600519"])
    assert result["status"] == "success"
    assert result["rows"] == 2
    rows = db_session.scalars(
        select(StockFinancialIndicator).order_by(StockFinancialIndicator.report_date)
    ).all()
    assert rows[0].report_date == date(2023, 12, 31)
    assert rows[0].available_at is not None
    assert "摊薄每股收益(元)" in rows[0].payload


def test_import_stocktoday_valuations(
    db_session: Session, tmp_path: Path
) -> None:
    snapshot_root = tmp_path / "tushare_snapshot"
    partition = snapshot_root / "stocks" / "daily_basic"
    partition.mkdir(parents=True)
    pd.DataFrame(
        [
            {
                "ts_code": "600519.SH",
                "trade_date": "20240102",
                "pe_ttm": 25.2,
                "pb": 8.1,
            },
            {
                "ts_code": "600519.SH",
                "trade_date": "20240103",
                "pe_ttm": 25.5,
                "pb": 8.2,
            },
        ]
    ).to_parquet(partition / "600519.SH.parquet", index=False)

    result = stock_fundamentals.import_stocktoday_valuations(
        db_session, snapshot_root
    )
    assert result["status"] == "success"
    assert result["codes"] == 1
    assert result["rows"] == 4
    rows = db_session.scalars(
        select(StockValuation).order_by(
            StockValuation.trade_date, StockValuation.indicator
        )
    ).all()
    assert len(rows) == 4
    assert {row.source for row in rows} == {"stocktoday"}

    repeated = stock_fundamentals.import_stocktoday_valuations(
        db_session, snapshot_root
    )
    assert repeated["rows"] == 4
    assert len(db_session.scalars(select(StockValuation)).all()) == 4


def test_import_stocktoday_financial_indicators(
    db_session: Session, tmp_path: Path
) -> None:
    snapshot_root = tmp_path / "tushare_snapshot"
    partition = snapshot_root / "stocks" / "fina_indicator"
    partition.mkdir(parents=True)
    pd.DataFrame(
        [
            {
                "ts_code": "600519.SH",
                "ann_date": "20240420",
                "end_date": "20231231",
                "eps": 2.1,
                "roe": 18.2,
                "grossprofit_margin": 51.0,
            },
            {
                "ts_code": "600519.SH",
                "ann_date": "20240510",
                "end_date": "20231231",
                "eps": 2.2,
                "roe": 18.5,
                "grossprofit_margin": 52.0,
            },
            {
                "ts_code": "600519.SH",
                "ann_date": "20240428",
                "end_date": "20240331",
                "eps": 0.6,
                "roe": 4.8,
                "grossprofit_margin": 50.0,
            },
        ]
    ).to_parquet(partition / "600519.SH.parquet", index=False)

    result = stock_fundamentals.import_stocktoday_financial_indicators(
        db_session, snapshot_root
    )
    assert result["status"] == "success"
    assert result["codes"] == 1
    assert result["rows"] == 2
    rows = db_session.scalars(
        select(StockFinancialIndicator).order_by(
            StockFinancialIndicator.report_date
        )
    ).all()
    assert len(rows) == 2
    assert float(rows[0].eps) == pytest.approx(2.1)
    assert rows[0].available_at.date() == date(2024, 4, 20)
    assert rows[0].source == "stocktoday"
    assert json.loads(rows[0].payload)["grossprofit_margin"] == 51.0


def test_import_stocktoday_name_history(
    db_session: Session, tmp_path: Path
) -> None:
    snapshot_root = tmp_path / "tushare_snapshot"
    partition = snapshot_root / "stocks" / "namechange"
    partition.mkdir(parents=True)
    pd.DataFrame(
        [
            {
                "ts_code": "600001.SH",
                "name": "*ST测试",
                "start_date": "20200102",
                "end_date": "20210630",
                "change_reason": "实施退市风险警示",
            },
            {
                "ts_code": "600001.SH",
                "name": "测试股份",
                "start_date": "20210701",
                "end_date": None,
                "change_reason": "撤销退市风险警示",
            },
        ]
    ).to_parquet(partition / "600001.SH.parquet", index=False)

    result = stock_fundamentals.import_stocktoday_name_history(
        db_session, snapshot_root
    )
    assert result["status"] == "success"
    assert result["rows"] == 2
    rows = db_session.scalars(
        select(StockNameHistory).order_by(StockNameHistory.start_date)
    ).all()
    assert [row.is_st for row in rows] == [True, False]
    assert rows[0].end_date == date(2021, 6, 30)
    assert {row.source for row in rows} == {"stocktoday"}


def test_import_stocktoday_industries(
    db_session: Session, tmp_path: Path
) -> None:
    snapshot_root = tmp_path / "tushare_snapshot"
    partition = snapshot_root / "stocks" / "index_member_all"
    partition.mkdir(parents=True)
    pd.DataFrame(
        [
            {
                "ts_code": "600519.SH",
                "name": "贵州茅台",
                "l1_code": "801120.SI",
                "l1_name": "食品饮料",
                "l2_code": "801125.SI",
                "l2_name": "白酒Ⅱ",
                "l3_code": "851251.SI",
                "l3_name": "白酒Ⅲ",
                "is_new": "Y",
            }
        ]
    ).to_parquet(partition / "600519.SH.parquet", index=False)

    result = stock_fundamentals.import_stocktoday_industries(
        db_session, snapshot_root
    )
    assert result["status"] == "success"
    assert result["rows"] == 1
    row = db_session.scalar(select(StockIndustry))
    assert row is not None
    assert row.industry_name == "食品饮料"
    assert row.industry_code == "801120.SI"
    assert row.source == "stocktoday_sw2021"


def test_sync_financial_uses_eastmoney_normalized_fields(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        ak_fetch,
        "fetch_financial_indicator_eastmoney",
        lambda symbol: pd.DataFrame(
            [
                {
                    "REPORT_DATE": "2026-06-30",
                    "NOTICE_DATE": "2026-07-31",
                    "EPSJB": 0.22,
                    "ROEJQ": 6.41,
                    "XSMLL": 29.21,
                    "ZCFZL": 59.17,
                    "PARENTNETPROFIT": 833_852_270.0,
                    "NCO_NETPROFIT": 0.72,
                }
            ]
        ),
    )
    monkeypatch.setattr(
        ak_fetch,
        "fetch_financial_indicator",
        lambda symbol: pytest.fail("东财成功时不应调用新浪"),
    )
    monkeypatch.setattr(
        ak_fetch,
        "fetch_financial_indicator_ths",
        lambda symbol: pytest.fail("东财成功时不应调用同花顺"),
    )

    result = stock_fundamentals.sync_financial_indicators(
        db_session, ["000683"]
    )

    assert result["status"] == "success"
    assert result["sources"] == {"eastmoney": 1}
    row = db_session.scalar(
        select(StockFinancialIndicator).where(
            StockFinancialIndicator.code == "000683"
        )
    )
    assert row is not None
    assert row.source == "eastmoney"
    assert float(row.roe) == pytest.approx(6.41)
    assert row.available_at is not None
    assert '"销售毛利率(%)": 29.21' in row.payload


def test_sync_disclosure_available_at(db_session: Session, mock_ak: None) -> None:
    stock_data.sync_stock_master(db_session)
    result = stock_fundamentals.sync_report_disclosure(db_session, ["600519"], ["20231231"])
    assert result["status"] == "success"
    row = db_session.scalar(
        select(StockReportDisclosure).where(StockReportDisclosure.code == "600519")
    )
    assert row is not None
    assert row.report_date == date(2023, 12, 31)
    assert row.disclosure_date == date(2024, 4, 26)
    assert row.estimate_date == date(2024, 4, 20)
    # available_at 取披露日 15:00
    assert row.available_at is not None and row.available_at.hour == 15


def test_disclosure_market_period_snapshot_fetch(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """接口适配：每个 (market 分区, period) 只抓一次全市场快照，按 code 分配。"""
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        ak_fetch, "fetch_stock_code_name", lambda: _master_frame()
    )
    stock_data.sync_stock_master(db_session)
    monkeypatch.setattr(
        ak_fetch,
        "fetch_report_disclosure",
        lambda market, period: calls.append((market, period)) or _disclosure_frame(market),
    )
    result = stock_fundamentals.sync_report_disclosure(db_session, None, ["20231231"])
    assert result["status"] == "success"
    # 沪市/深市/北交所 三个分区各抓一次，而不是每股一次
    markets = {market for market, _ in calls}
    assert markets == {"沪市", "深市", "北交所"}
    assert len(calls) == 3
    # 按 code 分配入库：北交所 92 号段也覆盖
    rows = db_session.scalars(select(StockReportDisclosure)).all()
    assert {r.code for r in rows} == {"600519", "000001", "920001"}
    bj = next(r for r in rows if r.code == "920001")
    assert bj.disclosure_date == date(2024, 4, 28)


def test_disclosure_period_normalization() -> None:
    assert stock_fundamentals._normalize_period("20231231") == ("2023年报", date(2023, 12, 31))
    assert stock_fundamentals._normalize_period("2024-03-31") == ("2024一季", date(2024, 3, 31))
    assert stock_fundamentals._normalize_period("2024半年报") == ("2024半年报", date(2024, 6, 30))
    assert stock_fundamentals._normalize_period("2024三季") == ("2024三季", date(2024, 9, 30))
    assert stock_fundamentals._normalize_period("20240101") is None  # 非法定报告期
    assert stock_fundamentals._normalize_period("胡说") is None


def test_disclosure_partial_when_one_market_fails(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """单分区抓取失败 -> partial，失败摘要落 stock_sync_state.detail。"""
    monkeypatch.setattr(
        ak_fetch, "fetch_stock_code_name", lambda: _master_frame()
    )
    stock_data.sync_stock_master(db_session)

    def flaky(market: str, period: str) -> pd.DataFrame | None:
        return None if market == "深市" else _disclosure_frame(market)

    monkeypatch.setattr(ak_fetch, "fetch_report_disclosure", flaky)
    result = stock_fundamentals.sync_report_disclosure(db_session, None, ["20231231"])
    assert result["status"] == "partial"
    assert result["failed"] == 1 and result["updated"] == 2
    state = db_session.get(StockSyncState, "disclosure")
    assert state.status == "partial"
    assert "深市" in (state.detail or "")


def test_disclosure_without_master_falls_back_to_whole_market(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """master 为空时退化为单次全市场（沪深京）快照。"""
    calls: list[str] = []
    monkeypatch.setattr(
        ak_fetch,
        "fetch_report_disclosure",
        lambda market, period: calls.append(market) or _disclosure_frame(market),
    )
    result = stock_fundamentals.sync_report_disclosure(db_session, None, ["20231231"])
    assert result["status"] == "success"
    assert calls == ["沪深京"]
    rows = db_session.scalars(select(StockReportDisclosure)).all()
    assert {r.code for r in rows} == {"600519", "000001"}


def test_sync_valuation(db_session: Session, mock_ak: None) -> None:
    result = stock_fundamentals.sync_valuations(db_session, ["600519"], indicators=["市盈率(TTM)"])
    assert result["status"] == "success"
    assert result["rows"] == 2
    rows = db_session.scalars(select(StockValuation)).all()
    assert {r.indicator for r in rows} == {"pe_ttm"}
    # 幂等：重复同步不产生重复行
    stock_fundamentals.sync_valuations(db_session, ["600519"], indicators=["市盈率(TTM)"])
    assert len(db_session.scalars(select(StockValuation)).all()) == 2


def test_industry_cninfo_current_columns(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ak_fetch, "fetch_industry_boards", lambda: None)
    monkeypatch.setattr(ak_fetch, "fetch_stock_profile_cninfo", lambda code: None)
    frame = pd.DataFrame(
        [
            {"行业中类": "银行", "行业大类": "银行", "变更日期": date(2021, 1, 1)},
            {"行业中类": None, "行业大类": "货币金融服务", "变更日期": date(2024, 1, 1)},
        ]
    )
    monkeypatch.setattr(ak_fetch, "fetch_industry_change_cninfo", lambda code: frame)
    result = stock_fundamentals.sync_industries(db_session, ["000001"])
    assert result["status"] == "success"
    assert result["stocks"] == 1
    industry = db_session.scalar(select(StockIndustry))
    assert industry is not None and industry.industry_name == "货币金融服务"


def test_sync_valuation_partial_sources(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    frame = _valuation_frame()

    def fetch(symbol: str, indicator: str, period: str = "近一年"):
        return frame if indicator == "市盈率(TTM)" else None

    monkeypatch.setattr(ak_fetch, "fetch_valuation_baidu", fetch)
    result = stock_fundamentals.sync_valuations(
        db_session, ["600519"], indicators=["市盈率(TTM)", "市净率"]
    )
    assert result["status"] == "partial"
    assert result["updated"] == 1
    assert result["failed"] == 1
    assert result["rows"] == 2


def test_sync_market_valuations_builds_pb_from_financial_bps(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_master(db_session, ["600519"])
    db_session.add(
        IndexConstituent(
            index_code="000300", stock_code="600519", stock_name="贵州茅台"
        )
    )
    db_session.add(
        StockFinancialIndicator(
            code="600519",
            report_date=date(2024, 3, 31),
            roe=20,
            payload='{"BPS": 10}',
            source="eastmoney",
        )
    )
    db_session.commit()
    monkeypatch.setattr(
        ak_fetch,
        "fetch_stock_spot_tencent",
        lambda: pd.DataFrame(
            [{"code": "sh600519", "zxj": "25", "pe_ttm": "12.5"}]
        ),
    )

    result = stock_fundamentals.sync_market_valuations(
        db_session, date(2024, 4, 30)
    )

    assert result["status"] == "success"
    rows = db_session.scalars(select(StockValuation)).all()
    assert {(row.indicator, float(row.value)) for row in rows} == {
        ("pe_ttm", 12.5),
        ("pb", 2.5),
    }
    assert {row.source for row in rows} == {"tencent_close"}


def test_sync_name_history_st_flag(db_session: Session, mock_ak: None) -> None:
    result = stock_fundamentals.sync_name_history(db_session, ["600519"])
    assert result["status"] == "success"
    rows = db_session.scalars(
        select(StockNameHistory).order_by(StockNameHistory.sort_order)
    ).all()
    assert len(rows) == 3
    # stock_info_change_name 解析：区间标记 "ST某某->某某股份" 展开为两段
    assert [r.name for r in rows] == ["某某股份", "ST某某", "某某股份"]
    assert rows[1].is_st is True
    assert rows[0].is_st is False
    # 接口不带日期：start_date 留空，区间顺序靠 sort_order，最新段 end_date 为空
    assert rows[0].start_date is None
    assert rows[-1].end_date is None
    assert [r.sort_order for r in rows] == [0, 1, 2]

    # 幂等：再次同步不产生重复行
    stock_fundamentals.sync_name_history(db_session, ["600519"])
    assert len(db_session.scalars(select(StockNameHistory)).all()) == 3


def test_name_history_explicit_dates_upsert(db_session: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    """带显式日期的数据（旧接口格式）按 (code, start_date, name) 幂等。"""
    frame = pd.DataFrame(
        [
            {"名称": "旧名", "开始日期": "2020-01-01", "结束日期": "2021-05-01", "变更原因": None},
            {"名称": "ST新名", "开始日期": "2021-05-02", "结束日期": None, "变更原因": "实施风险警示"},
        ]
    )
    monkeypatch.setattr(ak_fetch, "fetch_name_change_hist", lambda symbol: frame)
    result = stock_fundamentals.sync_name_history(db_session, ["600519"])
    assert result["rows"] == 2
    rows = db_session.scalars(
        select(StockNameHistory).order_by(StockNameHistory.start_date)
    ).all()
    assert rows[0].start_date == date(2020, 1, 1)
    assert rows[0].end_date == date(2021, 5, 1)
    assert rows[1].is_st is True
    assert rows[1].change_reason == "实施风险警示"


def test_extract_name_segments() -> None:
    assert stock_fundamentals._extract_name_segments("A->B->C") == ["A", "B", "C"]
    assert stock_fundamentals._extract_name_segments("中科健A(不含ST)") == ["中科健A"]
    assert stock_fundamentals._extract_name_segments("A/A") == ["A"]  # 连续重复段合并
    assert stock_fundamentals._extract_name_segments("") == []


def test_fundamentals_network_failure(db_session: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        ak_fetch, "fetch_financial_indicator_eastmoney", lambda symbol: None
    )
    monkeypatch.setattr(ak_fetch, "fetch_financial_indicator", lambda symbol: None)
    monkeypatch.setattr(
        ak_fetch, "fetch_financial_indicator_ths", lambda symbol: None
    )
    result = stock_fundamentals.sync_financial_indicators(db_session, ["600519"])
    assert result["status"] == "failed"
    assert result["errors"]


# ---------------------------------------------------------------------------
# 行业归属
# ---------------------------------------------------------------------------

def test_sync_industries_primary_source(db_session: Session, mock_ak: None) -> None:
    result = stock_fundamentals.sync_industries(db_session)
    assert result["status"] == "success"
    assert result["stocks"] == 2
    rows = db_session.scalars(select(StockIndustry).order_by(StockIndustry.code)).all()
    assert {r.code for r in rows} == {"600519", "000001"}
    assert all(r.source == "em" for r in rows)
    assert next(r for r in rows if r.code == "600519").industry_name == "酿酒行业"


def test_sync_industries_fallback_when_boards_unavailable(
    db_session: Session, monkeypatch: pytest.MonkeyPatch, mock_ak: None
) -> None:
    """主源不可用 -> 巨潮个股行业变动回退。"""
    stock_data.sync_stock_master(db_session)
    monkeypatch.setattr(ak_fetch, "fetch_industry_boards", lambda: None)
    monkeypatch.setattr(
        ak_fetch,
        "fetch_industry_change_cninfo",
        lambda symbol: pd.DataFrame([{"行业名称": "白酒"}]) if symbol == "600519" else None,
    )
    monkeypatch.setattr(ak_fetch, "fetch_stock_profile_cninfo", lambda symbol: None)
    result = stock_fundamentals.sync_industries(db_session, ["600519", "000001"])
    # 600519 回退成功，000001 回退失败 -> partial
    assert result["status"] == "partial"
    row = db_session.scalar(
        select(StockIndustry).where(
            StockIndustry.code == "600519", StockIndustry.source == "cninfo"
        )
    )
    assert row is not None and row.industry_name == "白酒"
    state = db_session.get(StockSyncState, "industry")
    assert state.status == "partial"
    assert "000001" in (state.detail or "")


# ---------------------------------------------------------------------------
# SqlStockRepository：估值 PIT / 披露兜底 / 历史 ST / 双口径面板
# ---------------------------------------------------------------------------


def _seed_master(db_session: Session, codes: list[str]) -> None:
    for code in codes:
        db_session.add(StockMaster(code=code, name=f"股票{code}"))
    db_session.commit()


def test_repository_valuation_as_of_filter(db_session: Session) -> None:
    """估值按 as_of 过滤：未来 PE/PB 不得进入历史快照（无未来估值泄漏）。"""
    from app.services.stock_repository import SqlStockRepository

    _seed_master(db_session, ["600519"])
    db_session.add(
        StockFinancialIndicator(
            code="600519", report_date=date(2023, 12, 31), roe=25.0, payload="{}"
        )
    )
    # 两个估值点：2024-03-01（as_of 内）与 2024-09-01（as_of 外）
    for day, value in ((date(2024, 3, 1), 20.0), (date(2024, 9, 1), 5.0)):
        db_session.add(
            StockValuation(code="600519", trade_date=day, indicator="pe_ttm", value=value)
        )
    db_session.commit()

    repo = SqlStockRepository(db_session)
    snaps = repo.fundamentals(["600519"], as_of=date(2024, 6, 1))
    assert len(snaps) == 1
    # as_of=2024-06-01 只能看到 3-01 的 PE=20 → EP=0.05；9-01 的 PE=5 不可见
    assert snaps[0].ep == pytest.approx(1.0 / 20.0)
    # 不过滤 as_of 时取最新（9-01, PE=5）→ EP=0.2
    snaps_all = repo.fundamentals(["600519"], None)
    assert snaps_all[0].ep == pytest.approx(1.0 / 5.0)


def test_repository_disclosure_statutory_fallback(
    db_session: Session, caplog: pytest.LogCaptureFixture
) -> None:
    """披露日/入库时间皆缺失：available_at 按法定最晚披露日保守估计并 warning。"""
    import logging

    from app.services.stock_repository import SqlStockRepository, statutory_disclosure_deadline

    _seed_master(db_session, ["600519"])
    db_session.add(
        StockFinancialIndicator(
            code="600519", report_date=date(2023, 12, 31), roe=25.0,
            payload="{}", available_at=None,
        )
    )
    db_session.commit()

    repo = SqlStockRepository(db_session)
    with caplog.at_level(logging.WARNING):
        snaps = repo.fundamentals(["600519"], None)
    assert len(snaps) == 1
    # 年报法定最晚披露日：次年 4-30
    assert snaps[0].available_at == date(2024, 4, 30)
    assert snaps[0].available_at == statutory_disclosure_deadline(date(2023, 12, 31))
    assert any("法定" in record.message for record in caplog.records)
    # PIT 语义：as_of 早于兜底日 → 快照不可见
    assert repo.fundamentals(["600519"], as_of=date(2024, 4, 29)) == []
    assert len(repo.fundamentals(["600519"], as_of=date(2024, 4, 30))) == 1


def test_repository_name_histories_as_of(db_session: Session) -> None:
    """历史 ST 用名称区间 as_of 判定；无精确日期的区间不参与（不伪造）。"""
    from app.services.stock_repository import SqlStockRepository

    _seed_master(db_session, ["600519"])
    db_session.add(
        StockNameHistory(
            code="600519", name="ST茅台", start_date=date(2021, 1, 1),
            end_date=date(2021, 12, 31), is_st=True, sort_order=0,
        )
    )
    db_session.add(
        StockNameHistory(
            code="600519", name="无日期区间", start_date=None,
            end_date=None, is_st=True, sort_order=1,
        )
    )
    db_session.commit()

    repo = SqlStockRepository(db_session)
    histories = repo.name_histories(["600519"])
    periods = histories["600519"]
    # 无精确日期的区间被跳过
    assert len(periods) == 1 and periods[0].is_st is True
    assert periods[0].start_date == date(2021, 1, 1)


def test_repository_industry_from_stock_industry(db_session: Session) -> None:
    """行业映射接入 StockIndustry 表：list_stocks 带出行业（不再全未知）。"""
    from app.services.stock_repository import SqlStockRepository

    _seed_master(db_session, ["600519", "000001"])
    db_session.add(
        StockIndustry(code="600519", name="贵州茅台", source="em", industry_name="白酒")
    )
    db_session.add(
        StockIndustry(
            code="600519",
            name="贵州茅台",
            source="stocktoday_sw2021",
            industry_name="食品饮料",
        )
    )
    db_session.commit()

    repo = SqlStockRepository(db_session)
    infos = {info.code: info for info in repo.list_stocks(None)}
    assert infos["600519"].industry == "食品饮料"
    assert infos["000001"].industry == "未知"  # 未覆盖的降级为未知


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------

def test_get_data_status(db_session: Session, mock_ak: None, research_root: Path) -> None:
    stock_data.sync_stock_master(db_session)
    stock_data.sync_stock_daily(db_session, ["600519"], fetch_qfq=False)
    stock_universe.sync_index_cons(db_session, ["000300"])
    stock_fundamentals.sync_financial_indicators(db_session, ["600519"])
    stock_fundamentals.sync_valuations(db_session, ["600519"], indicators=["市净率"])

    status = stock_data.get_data_status(db_session)
    assert status["master"]["stocks"] == 4
    assert status["daily"]["stocks_tracked"] == 1
    assert status["daily"]["stocks_with_parquet"] == 1
    assert status["daily"]["first_trade_date"] == date(2024, 1, 2)
    assert status["universe"]["constituents"] == {"000300": 2}
    assert status["fundamentals"]["financial_indicator_rows"] == 2
    assert status["fundamentals"]["valuation_rows"] == 2
    assert status["fundamentals"]["sync_valuation"]["status"] == "success"
    assert status["industry"]["sync"]["status"] == "never_run"
