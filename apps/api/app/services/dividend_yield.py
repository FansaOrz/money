"""PIT 过去十二个月税前现金股息率。

正式口径：
- 分子为信号日前 365 天内已经除权、且当时已公开的每股税前现金分红；
- 普通股息和特别股息均计入，股票股利不计入；
- 分母为信号日不晚于当日的最新未复权收盘价；
- 无分红只有在该股票分红源覆盖已确认时才解释为 0，否则保持缺失。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Iterable

DIVIDEND_COVERAGE_MINIMUM = 0.80


@dataclass(frozen=True)
class DividendEvent:
    code: str
    ex_date: date
    available_at: date
    cash_per_share: float
    event_key: str
    source_hash: str = ""
    revision: int = 1


@dataclass(frozen=True)
class DividendYieldResult:
    value: float | None
    trailing_cash_per_share: float | None
    status: str
    reason: str
    event_count: int
    source_hashes: tuple[str, ...] = ()


@dataclass(frozen=True)
class DividendCoverageReport:
    total: int
    valid: int
    coverage: float
    minimum: float
    passed: bool
    missing_by_status: dict[str, int]


def assess_dividend_coverage(
    results: Iterable[DividendYieldResult],
    *,
    minimum: float = DIVIDEND_COVERAGE_MINIMUM,
) -> DividendCoverageReport:
    items = list(results)
    missing: dict[str, int] = {}
    valid = 0
    for item in items:
        if item.value is not None and item.status in {"valid", "provider_fallback"}:
            valid += 1
        else:
            missing[item.status] = missing.get(item.status, 0) + 1
    coverage = valid / len(items) if items else 0.0
    return DividendCoverageReport(
        total=len(items),
        valid=valid,
        coverage=coverage,
        minimum=minimum,
        passed=bool(items) and coverage >= minimum,
        missing_by_status=missing,
    )


def calculate_trailing_dividend_yield(
    events: Iterable[DividendEvent],
    *,
    code: str,
    as_of: date,
    price: float | None,
    source_covered: bool,
    lookback_days: int = 365,
) -> DividendYieldResult:
    """计算单只股票的 PIT trailing dividend yield。"""
    start = as_of - timedelta(days=lookback_days)
    chosen: dict[str, DividendEvent] = {}
    for event in events:
        if event.code != code:
            continue
        if event.available_at > as_of or not (start < event.ex_date <= as_of):
            continue
        if event.cash_per_share <= 0:
            continue
        previous = chosen.get(event.event_key)
        if previous is None or (
            event.revision,
            event.available_at,
        ) > (
            previous.revision,
            previous.available_at,
        ):
            chosen[event.event_key] = event
    if price is None or price <= 0:
        return DividendYieldResult(
            value=None,
            trailing_cash_per_share=None,
            status="missing_price",
            reason="信号日不晚于当日的未复权收盘价缺失",
            event_count=len(chosen),
        )
    if not chosen and not source_covered:
        return DividendYieldResult(
            value=None,
            trailing_cash_per_share=None,
            status="source_uncovered",
            reason="分红主数据未确认覆盖，不能把未知误记为零分红",
            event_count=0,
        )
    trailing_cash = sum(item.cash_per_share for item in chosen.values())
    return DividendYieldResult(
        value=trailing_cash / price,
        trailing_cash_per_share=trailing_cash,
        status="valid",
        reason=(
            "过去365天无已公开且已除权的税前现金分红"
            if not chosen
            else "过去365天已公开且已除权的税前现金分红/未复权收盘价"
        ),
        event_count=len(chosen),
        source_hashes=tuple(
            sorted({item.source_hash for item in chosen.values() if item.source_hash})
        ),
    )


def load_normalized_dividend_events(
    db: object,
    *,
    codes: list[str],
    as_of: date,
    lookback_days: int = 365,
) -> list[DividendEvent]:
    """从不可变规范化公司行为主数据读取可见版本。"""
    from sqlalchemy import select

    from app.models import QuantDataRecord

    start = as_of - timedelta(days=lookback_days)
    statement = select(QuantDataRecord).where(
        QuantDataRecord.dataset == "corporate_action",
        QuantDataRecord.code.in_(codes),
        QuantDataRecord.effective_date > start,
        QuantDataRecord.effective_date <= as_of,
    )
    rows = db.scalars(statement).all()  # type: ignore[attr-defined]
    result: list[DividendEvent] = []
    for row in rows:
        payload = dict(row.payload or {})
        if payload.get("kind") != "cash_entitlement":
            continue
        if payload.get("resolution_status") in {
            "conflict",
            "rejected",
            "superseded",
        }:
            continue
        available = row.available_at
        available_date = (
            available.date() if isinstance(available, datetime) else available
        )
        cash = payload.get("cash_per_share")
        try:
            cash_value = float(cash)
        except (TypeError, ValueError):
            continue
        result.append(
            DividendEvent(
                code=row.code,
                ex_date=row.effective_date,
                available_at=available_date,
                cash_per_share=cash_value,
                event_key=str(payload.get("event_key") or row.id),
                source_hash=row.source_hash,
                revision=int(payload.get("revision") or 1),
            )
        )
    return result
