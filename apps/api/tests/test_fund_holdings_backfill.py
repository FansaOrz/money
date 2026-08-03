"""候选池基金季度重仓/行业披露回填测试。

覆盖：
- mock 数据源下的正常写入（重仓 + 行业）与状态行；
- 幂等：重跑同年不产生重复行，complete 基金被跳过；
- 候选池过滤：只处理指定池的 active 成员，--codes 进一步收窄；
- 失败继续：单只抓取异常不影响后续基金，failed 状态可下轮重试；
- dry-run 不发起请求、不写状态。
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import (
    CandidatePool,
    CandidatePoolMember,
    FundHolding,
    FundHoldingsSyncStatus,
    FundIndustryAllocation,
    Instrument,
    InstrumentType,
)
from app.services import fund_holdings_backfill as backfill

YEAR = 2025
REPORT_DATE = date(2025, 6, 30)


def _make_holdings(code: str) -> list[dict]:
    return [
        {
            "report_date": REPORT_DATE,
            "rank": 1,
            "stock_code": "600519",
            "stock_name": "贵州茅台",
            "weight": Decimal("5.25"),
            "shares": Decimal("100.0"),
            "market_value": Decimal("1000.0"),
        }
    ]


def _make_industries(code: str) -> list[dict]:
    return [
        {
            "report_date": REPORT_DATE,
            "industry": "食品饮料",
            "weight": Decimal("20.5"),
            "market_value": Decimal("2000.0"),
        }
    ]


class FakeSource:
    """可编程假数据源：按 code 返回固定数据或抛异常。"""

    def __init__(
        self,
        *,
        fail_on: set[str] | None = None,
        empty_holdings: set[str] | None = None,
        empty_industries: set[str] | None = None,
    ) -> None:
        self.fail_on = fail_on or set()
        self.empty_holdings = empty_holdings or set()
        self.empty_industries = empty_industries or set()
        self.holding_calls: list[str] = []
        self.industry_calls: list[str] = []

    def fetch_holdings(self, code: str, year: int) -> list[dict]:
        self.holding_calls.append(code)
        if code in self.fail_on:
            raise RuntimeError("source down")
        if code in self.empty_holdings:
            return []
        return _make_holdings(code)

    def fetch_industries(self, code: str, year: int) -> list[dict]:
        self.industry_calls.append(code)
        if code in self.empty_industries:
            return []
        return _make_industries(code)


def _seed_pool(
    db_session: Session,
    codes: list[str],
    *,
    excluded: set[str] | None = None,
) -> CandidatePool:
    """建一个候选池：每个 code 对应一只 Instrument 与一个成员行。"""
    pool = CandidatePool(name="测试池", max_size=800, member_count=len(codes))
    db_session.add(pool)
    db_session.flush()
    excluded = excluded or set()
    for rank, code in enumerate(codes):
        instrument = Instrument(
            code=code, name=f"基金{code}", type=InstrumentType.FUND, currency="CNY"
        )
        db_session.add(instrument)
        db_session.add(
            CandidatePoolMember(
                pool_id=pool.id,
                code=code,
                name=f"基金{code}",
                tier=1,
                rank=rank,
                status="excluded" if code in excluded else "active",
            )
        )
    db_session.commit()
    return pool


def _status_of(db_session: Session, code: str, year: int = YEAR) -> FundHoldingsSyncStatus | None:
    return db_session.scalar(
        select(FundHoldingsSyncStatus)
        .join(Instrument, Instrument.id == FundHoldingsSyncStatus.instrument_id)
        .where(Instrument.code == code, FundHoldingsSyncStatus.year == year)
    )


def test_backfill_writes_holdings_and_industries(db_session: Session) -> None:
    """mock 源下正常写入重仓与行业，状态行 complete。"""
    _seed_pool(db_session, ["110022", "110023"])
    source = FakeSource()
    result = backfill.backfill_fund_holdings(
        db_session, YEAR, source=source, sleep_seconds=0
    )
    assert result["processed"] == 2
    assert result["complete"] == 2
    assert result["holding_rows"] == 2
    assert result["industry_rows"] == 2

    holdings = db_session.scalars(select(FundHolding)).all()
    industries = db_session.scalars(select(FundIndustryAllocation)).all()
    assert len(holdings) == 2
    assert len(industries) == 2
    assert {h.stock_code for h in holdings} == {"600519"}

    status = _status_of(db_session, "110022")
    assert status is not None
    assert status.status == "complete"
    assert status.holding_rows == 1
    assert status.industry_rows == 1
    assert status.last_error is None
    assert status.fetched_at is not None


def test_backfill_idempotent_and_skips_complete(db_session: Session) -> None:
    """重跑同年：行数不翻倍；complete 基金不再请求数据源。"""
    _seed_pool(db_session, ["110022", "110023"])
    source = FakeSource()
    backfill.backfill_fund_holdings(db_session, YEAR, source=source, sleep_seconds=0)
    assert db_session.scalar(select(func.count(FundHolding.id))) == 2

    # 第二轮：complete 全部跳过，数据源零调用
    source2 = FakeSource()
    result = backfill.backfill_fund_holdings(
        db_session, YEAR, source=source2, sleep_seconds=0
    )
    assert result["selected"] == 0
    assert result["processed"] == 0
    assert source2.holding_calls == []
    assert source2.industry_calls == []
    assert db_session.scalar(select(func.count(FundHolding.id))) == 2

    # 直接对单只重跑（模拟强制重试）：先删后插，不产生重复
    instrument = db_session.scalar(select(Instrument).where(Instrument.code == "110022"))
    again = backfill.backfill_one(db_session, instrument, YEAR, source=source2)
    assert again["status"] == "complete"
    assert db_session.scalar(select(func.count(FundHolding.id))) == 2


def test_backfill_filters_pool_and_codes(db_session: Session) -> None:
    """默认取最新池；--pool-id 指定旧池；excluded 成员与 --codes 外的基金不处理。"""
    old_pool = _seed_pool(db_session, ["110001", "110002"])
    _seed_pool(db_session, ["110003", "110004"], excluded={"110004"})

    # 默认最新池：110004 为 excluded，只处理 110003
    source = FakeSource()
    result = backfill.backfill_fund_holdings(
        db_session, YEAR, source=source, sleep_seconds=0
    )
    assert result["processed"] == 1
    assert source.holding_calls == ["110003"]

    # 指定旧池 + codes 收窄
    source2 = FakeSource()
    result2 = backfill.backfill_fund_holdings(
        db_session,
        YEAR,
        pool_id=old_pool.id,
        codes=["110002"],
        source=source2,
        sleep_seconds=0,
    )
    assert result2["pool_id"] == old_pool.id
    assert result2["processed"] == 1
    assert source2.holding_calls == ["110002"]


def test_backfill_continues_after_failure(db_session: Session) -> None:
    """单只抓取抛异常：记 failed 并继续后续基金；下轮重试 failed 而跳过 complete。"""
    _seed_pool(db_session, ["110011", "110012", "110013"])
    source = FakeSource(fail_on={"110012"})
    result = backfill.backfill_fund_holdings(
        db_session, YEAR, source=source, sleep_seconds=0
    )
    assert result["processed"] == 3
    assert result["complete"] == 2
    assert result["failed"] == 1

    failed_status = _status_of(db_session, "110012")
    assert failed_status is not None
    assert failed_status.status == "failed"
    assert "source down" in (failed_status.last_error or "")
    # 失败基金不写重仓/行业
    instrument = db_session.scalar(select(Instrument).where(Instrument.code == "110012"))
    assert (
        db_session.scalar(
            select(func.count(FundHolding.id)).where(
                FundHolding.instrument_id == instrument.id
            )
        )
        == 0
    )

    # 下轮：源恢复后仅重试 failed 的 110012
    source2 = FakeSource()
    result2 = backfill.backfill_fund_holdings(
        db_session, YEAR, source=source2, sleep_seconds=0
    )
    assert result2["processed"] == 1
    assert source2.holding_calls == ["110012"]
    assert _status_of(db_session, "110012").status == "complete"
    # 总行数仍为 3（每基金一条重仓）
    assert db_session.scalar(select(func.count(FundHolding.id))) == 3


def test_non_equity_empty_holdings_not_applicable(db_session: Session) -> None:
    pool = _seed_pool(db_session, ["BOND"])
    instrument = db_session.scalar(select(Instrument).where(Instrument.code == "BOND"))
    assert instrument is not None
    instrument.name = "测试纯债债券"
    db_session.commit()
    source = FakeSource(empty_holdings={"BOND"}, empty_industries={"BOND"})
    result = backfill.backfill_one(db_session, instrument, 2026, source=source)
    assert result["status"] == "not_applicable"
    assert backfill.select_pending(db_session, 2026, pool_id=pool.id) == []


def test_backfill_empty_results_mark_partial_and_failed(db_session: Session) -> None:
    """重仓为空 -> failed（可重试）；仅行业为空 -> partial；两者均会参与下轮。"""
    _seed_pool(db_session, ["110021", "110022"])
    source = FakeSource(empty_holdings={"110021"}, empty_industries={"110022"})
    result = backfill.backfill_fund_holdings(
        db_session, YEAR, source=source, sleep_seconds=0
    )
    assert result["failed"] == 1
    assert result["partial"] == 1
    assert _status_of(db_session, "110021").status == "failed"
    assert _status_of(db_session, "110021").last_error == "no holdings returned"
    assert _status_of(db_session, "110022").status == "partial"

    # 下轮源恢复：failed 与 partial 都被重新处理
    source2 = FakeSource()
    result2 = backfill.backfill_fund_holdings(
        db_session, YEAR, source=source2, sleep_seconds=0
    )
    assert result2["processed"] == 2
    assert result2["complete"] == 2


def test_backfill_limit_and_dry_run(db_session: Session) -> None:
    """limit 截断生效；dry-run 不请求数据源、不写状态行。"""
    _seed_pool(db_session, ["110031", "110032", "110033"])
    source = FakeSource()
    result = backfill.backfill_fund_holdings(
        db_session, YEAR, limit=2, source=source, sleep_seconds=0
    )
    assert result["processed"] == 2
    assert len(source.holding_calls) == 2

    dry = backfill.backfill_fund_holdings(
        db_session, YEAR + 1, dry_run=True, source=source, sleep_seconds=0
    )
    assert dry["dry_run"] is True
    assert dry["selected"] == 3
    assert all(r["state"] == "never" for r in dry["results"])
    # dry-run 未写新年度状态
    assert (
        db_session.scalar(
            select(func.count(FundHoldingsSyncStatus.id)).where(
                FundHoldingsSyncStatus.year == YEAR + 1
            )
        )
        == 0
    )


def test_backfill_year_independence(db_session: Session) -> None:
    """多年度状态独立：上一年 complete 不影响下一年待处理。"""
    _seed_pool(db_session, ["110041"])
    source = FakeSource()
    backfill.backfill_fund_holdings(db_session, YEAR, source=source, sleep_seconds=0)
    assert _status_of(db_session, "110041", YEAR).status == "complete"

    source2 = FakeSource()
    result = backfill.backfill_fund_holdings(
        db_session, YEAR - 1, source=source2, sleep_seconds=0
    )
    assert result["processed"] == 1
    assert source2.holding_calls == ["110041"]
    assert _status_of(db_session, "110041", YEAR - 1).status == "complete"
