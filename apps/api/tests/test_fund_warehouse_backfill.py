"""全市场基金净值 → 研究仓库回填（fund_warehouse_backfill）测试。

覆盖：
- 选批优先级：never → failed → 按最早净值日期优先，--limit/--codes 过滤；
- 单只回填：百分数 daily_growth_rate /100 写入 daily_return；
- available_at 不伪称历史可见（= 写入时刻），source 带 backfill_ 前缀；
- 幂等：重复执行仓库行数不翻倍，状态仍为 complete；
- 断点续传：complete 基金跳过，failed 基金下轮优先重试并恢复；
- 目录基金不要求、也不创建 Instrument；
- dry-run：不发起请求、不写仓库。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import FundCatalogEntry, FundWarehouseSyncState, Instrument
from app.research.repository import DuckDBRepository
from app.research.warehouse import ResearchWarehouse
from app.services.research import fund_warehouse_backfill as backfill


@pytest.fixture()
def warehouse(tmp_path: Path):
    wh = ResearchWarehouse(tmp_path / "research.duckdb", tmp_path / "lake")
    wh.init_schemas()
    yield wh
    wh.close()


@pytest.fixture()
def repo(warehouse: ResearchWarehouse) -> DuckDBRepository:
    return DuckDBRepository(warehouse)


def _add_catalog(db_session: Session, *codes: str) -> None:
    for code in codes:
        db_session.add(FundCatalogEntry(code=code, name=f"基金{code}", active=True))
    db_session.commit()


def _nav_rows(code: str, days: int = 3) -> list[dict]:
    base = date.today() - timedelta(days=10)
    return [
        {
            "code": code,
            "nav_date": base + timedelta(days=offset),
            "unit_nav": Decimal("1.0") + Decimal(offset) / 100,
            "accumulated_nav": Decimal("1.5"),
            # 百分数：1.5 表示 +1.5%
            "daily_growth_rate": Decimal("1.5"),
            "source": "eastmoney_fast",
        }
        for offset in range(days)
    ]


def _mock_fetch(monkeypatch: pytest.MonkeyPatch, rows_by_code: dict[str, list[dict]]) -> None:
    def fake_fetch(code: str, **kwargs):
        rows = rows_by_code.get(code, [])
        if rows:
            return rows, None, rows[0]["source"]
        return [], "所有历史净值源均未返回数据", None

    monkeypatch.setattr(backfill.fund_data, "fetch_nav_history_with_fallback", fake_fetch)


# ---------------------------------------------------------------------------
# 选批
# ---------------------------------------------------------------------------


def test_select_batch_priority_never_failed_oldest(db_session: Session) -> None:
    _add_catalog(db_session, "A1", "A2", "A3", "A4")
    today = date.today()
    # A2 failed，A3/A4 complete 且覆盖起点（earliest 越早优先级越高）
    db_session.add_all(
        [
            FundWarehouseSyncState(code="A2", status="failed", last_error="x"),
            FundWarehouseSyncState(
                code="A3",
                status="complete",
                target_start_date=today - timedelta(days=100),
                earliest_nav_date=today - timedelta(days=50),
                row_count=10,
            ),
            FundWarehouseSyncState(
                code="A4",
                status="complete",
                target_start_date=today - timedelta(days=100),
                earliest_nav_date=today - timedelta(days=80),
                row_count=20,
            ),
        ]
    )
    db_session.commit()

    batch = backfill.select_batch(db_session, limit=4)
    codes = [entry.code for entry, _ in batch]
    # failed(A2) 最优先（失败重试），其次 never(A1)，complete 中 earliest 更早的 A4 在 A3 前
    assert codes == ["A2", "A1", "A4", "A3"]
    states = {entry.code: state for entry, state in batch}
    assert states["A1"] is None  # never 无状态行


def test_select_batch_limit_and_codes(db_session: Session) -> None:
    _add_catalog(db_session, "B1", "B2", "B3")
    batch = backfill.select_batch(db_session, limit=2)
    assert len(batch) == 2

    batch = backfill.select_batch(db_session, limit=10, codes=["B3", "B1"])
    assert [entry.code for entry, _ in batch] == ["B1", "B3"]

    # 非 active 基金不入选批
    db_session.add(FundCatalogEntry(code="B9", name="停用", active=False))
    db_session.commit()
    batch = backfill.select_batch(db_session, limit=0)
    assert "B9" not in [entry.code for entry, _ in batch]


# ---------------------------------------------------------------------------
# 单只回填：口径 / 状态 / Instrument
# ---------------------------------------------------------------------------


def test_backfill_writes_daily_return_as_fraction(
    db_session: Session, repo: DuckDBRepository, monkeypatch: pytest.MonkeyPatch
) -> None:
    _add_catalog(db_session, "110022")
    _mock_fetch(monkeypatch, {"110022": _nav_rows("110022")})

    result = backfill.backfill_fund(db_session, repo, "110022", years=5)
    assert result["status"] == "complete"
    assert result["rows"] == 3
    assert result["source"] == "backfill_eastmoney_fast"

    frame = repo.read_fund_nav("110022")
    assert len(frame) == 3
    # 百分数 1.5 -> 小数 0.015
    assert frame["daily_return"].dropna().unique().tolist() == [pytest.approx(0.015)]
    assert frame["source"].unique().tolist() == ["backfill_eastmoney_fast"]
    # available_at 取写入时刻（最近 1 分钟内），不伪称历史可见
    for value in frame["available_at"]:
        assert datetime.now() - value.to_pydatetime() < timedelta(minutes=1)

    state = db_session.scalar(
        select(FundWarehouseSyncState).where(FundWarehouseSyncState.code == "110022")
    )
    assert state.status == "complete"
    assert state.row_count == 3
    assert state.earliest_nav_date == date.today() - timedelta(days=10)
    assert state.latest_nav_date == date.today() - timedelta(days=8)
    assert state.last_error is None


def test_backfill_does_not_require_or_create_instrument(
    db_session: Session, repo: DuckDBRepository, monkeypatch: pytest.MonkeyPatch
) -> None:
    _add_catalog(db_session, "000001")
    _mock_fetch(monkeypatch, {"000001": _nav_rows("000001", days=1)})

    before = db_session.scalar(select(func.count(Instrument.id)))
    result = backfill.backfill_fund(db_session, repo, "000001", years=5)
    after = db_session.scalar(select(func.count(Instrument.id)))
    assert result["status"] == "complete"
    assert before == after == 0  # 既不依赖也不创建 Instrument


def test_backfill_failure_records_state(
    db_session: Session, repo: DuckDBRepository, monkeypatch: pytest.MonkeyPatch
) -> None:
    _add_catalog(db_session, "F1")
    _mock_fetch(monkeypatch, {})  # 所有源失败

    result = backfill.backfill_fund(db_session, repo, "F1", years=5)
    assert result["status"] == "failed"
    assert result["error"]

    state = db_session.scalar(
        select(FundWarehouseSyncState).where(FundWarehouseSyncState.code == "F1")
    )
    assert state.status == "failed"
    assert "未返回数据" in state.last_error
    assert repo.read_fund_nav("F1").empty


# ---------------------------------------------------------------------------
# 幂等 / 断点续传
# ---------------------------------------------------------------------------


def test_backfill_idempotent_and_resume(
    db_session: Session, repo: DuckDBRepository, monkeypatch: pytest.MonkeyPatch
) -> None:
    _add_catalog(db_session, "110022")
    _mock_fetch(monkeypatch, {"110022": _nav_rows("110022")})

    first = backfill.backfill_fund(db_session, repo, "110022", years=5)
    assert first["status"] == "complete"
    rows_after_first = len(repo.read_fund_nav("110022"))

    # 重复执行：仓库写入按 (fund_code, effective_date) 幂等，行数不翻倍
    second = backfill.backfill_fund(db_session, repo, "110022", years=5)
    assert second["status"] == "complete"
    assert len(repo.read_fund_nav("110022")) == rows_after_first


def test_resume_skips_fully_covered_fund(
    db_session: Session, repo: DuckDBRepository, monkeypatch: pytest.MonkeyPatch
) -> None:
    _add_catalog(db_session, "110022")
    calls = {"n": 0}

    def counting_fetch(code: str, **kwargs):
        calls["n"] += 1
        return _nav_rows(code), None, "eastmoney_fast"

    monkeypatch.setattr(backfill.fund_data, "fetch_nav_history_with_fallback", counting_fetch)
    # 预置状态：已 complete 且 earliest 覆盖 5 年目标起点（resolve_window 按 365.25 天/年）
    target_start = date.today() - timedelta(days=int(5 * 365.25))
    db_session.add(
        FundWarehouseSyncState(
            code="110022",
            status="complete",
            target_start_date=target_start,
            earliest_nav_date=target_start,
            latest_nav_date=date.today(),
            row_count=1200,
        )
    )
    db_session.commit()

    result = backfill.backfill_fund(db_session, repo, "110022", years=5)
    assert result["status"] == "skipped"
    assert calls["n"] == 0  # 跳过时不再请求外部源

    # no-resume：忽略断点重新回填（仓库写幂等）
    result = backfill.backfill_fund(db_session, repo, "110022", years=5, resume=False)
    assert result["status"] == "complete"
    assert calls["n"] == 1


def test_failed_fund_retried_first_and_recovers(
    db_session: Session, repo: DuckDBRepository, monkeypatch: pytest.MonkeyPatch
) -> None:
    _add_catalog(db_session, "G1", "G2")
    _mock_fetch(monkeypatch, {})  # 第一轮全部失败
    summary = backfill.run_backfill(db_session, repo, limit=10)
    assert summary["failed"] == 2

    # 第二轮源恢复：failed 基金优先于 never 被选中
    done: list[str] = []

    def fake_fetch(code: str, **kwargs):
        done.append(code)
        return _nav_rows(code, days=2), None, "eastmoney"

    _add_catalog(db_session, "G3")  # 新增 never 基金
    monkeypatch.setattr(backfill.fund_data, "fetch_nav_history_with_fallback", fake_fetch)
    summary = backfill.run_backfill(db_session, repo, limit=2)
    assert summary["complete"] == 2
    assert done == ["G1", "G2"]  # failed 优先于 never(G3)
    assert len(repo.read_fund_nav("G1")) == 2


# ---------------------------------------------------------------------------
# 批量编排 / dry-run
# ---------------------------------------------------------------------------


def test_run_backfill_dry_run_no_fetch_no_write(
    db_session: Session, repo: DuckDBRepository, monkeypatch: pytest.MonkeyPatch
) -> None:
    _add_catalog(db_session, "D1", "D2")

    def forbidden_fetch(code: str, **kwargs):  # pragma: no cover - 不应被调用
        raise AssertionError("dry-run 不应请求外部源")

    monkeypatch.setattr(backfill.fund_data, "fetch_nav_history_with_fallback", forbidden_fetch)
    summary = backfill.run_backfill(db_session, repo, limit=10, dry_run=True)
    assert summary["dry_run"] is True
    assert summary["selected"] == 2
    assert {f["code"] for f in summary["funds"]} == {"D1", "D2"}
    assert all(f["state"] == "never" for f in summary["funds"])
    assert db_session.scalar(select(func.count(FundWarehouseSyncState.id))) == 0
    assert repo.read_fund_nav("D1").empty


def test_run_backfill_summary(
    db_session: Session, repo: DuckDBRepository, monkeypatch: pytest.MonkeyPatch
) -> None:
    _add_catalog(db_session, "S1", "S2", "S3")
    _mock_fetch(monkeypatch, {"S1": _nav_rows("S1", 2), "S2": _nav_rows("S2", 1)})

    summary = backfill.run_backfill(db_session, repo, limit=10, years=5)
    assert summary["selected"] == 3
    assert summary["complete"] == 2
    assert summary["failed"] == 1
    assert summary["rows"] == 3
    assert [f["code"] for f in summary["failures"]] == ["S3"]
