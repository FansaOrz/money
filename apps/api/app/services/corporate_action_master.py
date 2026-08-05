"""统一公司行为主数据导入、冲突工单、查询与更正回放。"""

from __future__ import annotations

from datetime import UTC, date, datetime, time
import hashlib
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import CorporateActionReviewCase, QuantDataRecord


EVENT_MODEL_VERSION = "CORPORATE_ACTION_EVENT_V2"
SUPPORTED_KINDS = {
    "cash_entitlement",
    "cash_payment",
    "share_distribution",
    "rights_issue",
    "merger",
    "cash_acquisition",
    "code_change",
    "terminal",
}


def _compact_date(value: object) -> date | None:
    text = str(value or "").replace("-", "")
    if len(text) != 8 or not text.isdigit():
        return None
    return date(int(text[:4]), int(text[4:6]), int(text[6:8]))


def _float(value: object) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _event_key(
    code: str,
    row: dict[str, object],
) -> str:
    return ":".join(
        (
            code,
            str(row.get("end_date") or ""),
            str(row.get("record_date") or ""),
            str(row.get("ex_date") or ""),
            str(row.get("imp_ann_date") or row.get("ann_date") or ""),
        )
    )


def import_dividend_snapshot(
    db: Session,
    snapshot_root: Path,
) -> dict[str, object]:
    """把静态 dividend Parquet 全量导入不可变统一事件主数据。"""
    import pyarrow.parquet as pq

    paths = sorted(snapshot_root.glob("*.parquet"))
    existing = {
        (
            record.code,
            record.effective_date,
            record.source,
            record.source_hash,
        )
        for record in db.scalars(
            select(QuantDataRecord).where(QuantDataRecord.dataset == "corporate_action")
        ).all()
    }
    inserted = skipped = invalid = 0
    hashes: set[str] = set()
    for path in paths:
        checksum = hashlib.sha256(path.read_bytes()).hexdigest()
        hashes.add(checksum)
        table = pq.read_table(path)
        for raw in table.to_pylist():
            row = dict(raw)
            if str(row.get("div_proc") or "") != "实施":
                continue
            code = str(row.get("ts_code") or path.stem).split(".")[0]
            ex_date = _compact_date(row.get("ex_date"))
            if ex_date is None:
                invalid += 1
                continue
            available_date = (
                _compact_date(row.get("imp_ann_date"))
                or _compact_date(row.get("ann_date"))
                or ex_date
            )
            available_at = datetime.combine(
                available_date,
                time(18, 0),
                tzinfo=UTC,
            )
            event_key = _event_key(code, row)
            cash = _float(row.get("cash_div"))
            if cash <= 0:
                cash = _float(row.get("cash_div_tax"))
            share_ratio = _float(row.get("stk_div"))
            events: list[tuple[str, date, dict[str, object]]] = []
            common: dict[str, object] = {
                "event_model_version": EVENT_MODEL_VERSION,
                "event_key": event_key,
                "record_date": (
                    _compact_date(row.get("record_date")).isoformat()
                    if _compact_date(row.get("record_date"))
                    else None
                ),
                "announcement_date": available_date.isoformat(),
                "implementation_announcement_date": (
                    _compact_date(row.get("imp_ann_date")).isoformat()
                    if _compact_date(row.get("imp_ann_date"))
                    else None
                ),
                "revision": 1,
                "resolution_status": "canonical",
                "field_sources": {
                    field: f"{path.name}#{field}" for field in table.schema.names
                },
            }
            if cash > 0:
                pay_date = _compact_date(row.get("pay_date"))
                events.append(
                    (
                        "cash_entitlement",
                        ex_date,
                        {
                            **common,
                            "cash_per_share": cash,
                            "cash_div_tax_reference": _float(row.get("cash_div_tax")),
                            "payment_date": (
                                pay_date.isoformat() if pay_date else None
                            ),
                        },
                    )
                )
                if pay_date is not None and pay_date > ex_date:
                    events.append(
                        (
                            "cash_payment",
                            pay_date,
                            {
                                **common,
                                "payment_date": pay_date.isoformat(),
                            },
                        )
                    )
            if share_ratio > 0:
                events.append(
                    (
                        "share_distribution",
                        ex_date,
                        {
                            **common,
                            "share_ratio": share_ratio,
                            "successor_listing_date": (
                                _compact_date(row.get("div_listdate")).isoformat()
                                if _compact_date(row.get("div_listdate"))
                                else None
                            ),
                            "fractional_handling": ("cash_if_official_else_restricted"),
                        },
                    )
                )
            for kind, effective_date, payload in events:
                source = f"tushare:dividend:{event_key}:{kind}"
                natural = (code, effective_date, source, checksum)
                if natural in existing:
                    skipped += 1
                    continue
                record = QuantDataRecord(
                    dataset="corporate_action",
                    code=code,
                    effective_date=effective_date,
                    available_at=available_at,
                    source=source,
                    source_file=str(path),
                    source_hash=checksum,
                    payload={"kind": kind, **payload},
                    imported_at=datetime.now(UTC),
                )
                db.add(record)
                existing.add(natural)
                inserted += 1
        if inserted and inserted % 5000 < 100:
            db.flush()
    db.commit()
    conflicts = detect_event_conflicts(db)
    return {
        "files": len(paths),
        "source_hashes": len(hashes),
        "inserted": inserted,
        "skipped": skipped,
        "invalid": invalid,
        "conflicts_opened": conflicts,
        "model_version": EVENT_MODEL_VERSION,
    }


def detect_event_conflicts(db: Session) -> int:
    """同一自然事件出现不同经济字段时开工单，不静默挑选版本。"""
    rows = db.scalars(
        select(QuantDataRecord).where(QuantDataRecord.dataset == "corporate_action")
    ).all()
    groups: dict[tuple[str, str, str], list[QuantDataRecord]] = {}
    for row in rows:
        payload = dict(row.payload or {})
        key = (
            row.code,
            str(payload.get("event_key") or row.effective_date),
            str(payload.get("kind") or ""),
        )
        groups.setdefault(key, []).append(row)
    opened = 0
    economic_fields = (
        "cash_per_share",
        "share_ratio",
        "subscription_ratio",
        "subscription_price",
        "successor_code",
        "terminal_price",
        "terminal_type",
        "cash_compensation_per_fraction",
    )
    for (code, event_key, kind), versions in groups.items():
        signatures = {
            tuple(dict(row.payload or {}).get(field) for field in economic_fields)
            for row in versions
        }
        if len(signatures) <= 1:
            continue
        existing = db.scalar(
            select(CorporateActionReviewCase).where(
                CorporateActionReviewCase.code == code,
                CorporateActionReviewCase.event_key == event_key,
                CorporateActionReviewCase.issue_type == "source_conflict",
            )
        )
        if existing is not None:
            continue
        latest = max(versions, key=lambda row: (row.available_at, row.id))
        db.add(
            CorporateActionReviewCase(
                record_id=latest.id,
                code=code,
                event_key=event_key,
                issue_type="source_conflict",
                status="open",
                reason=f"{kind} 同一事件存在不一致经济字段，禁止静默选择",
                conservative_value=0.0,
                evidence={
                    "record_ids": [row.id for row in versions],
                    "sources": [row.source for row in versions],
                    "source_hashes": [row.source_hash for row in versions],
                    "signatures": [list(value) for value in signatures],
                },
                resolution={},
                created_at=datetime.now(UTC),
            )
        )
        opened += 1
    if opened:
        db.commit()
    return opened


def event_timeline(
    db: Session,
    *,
    code: str,
    start: date | None = None,
    end: date | None = None,
    system_as_of: datetime | None = None,
) -> list[dict[str, object]]:
    statement = select(QuantDataRecord).where(
        QuantDataRecord.dataset == "corporate_action",
        QuantDataRecord.code == code.split(".")[0],
    )
    if start is not None:
        statement = statement.where(QuantDataRecord.effective_date >= start)
    if end is not None:
        statement = statement.where(QuantDataRecord.effective_date <= end)
    if system_as_of is not None:
        statement = statement.where(QuantDataRecord.imported_at <= system_as_of)
    rows = db.scalars(
        statement.order_by(
            QuantDataRecord.effective_date,
            QuantDataRecord.available_at,
            QuantDataRecord.id,
        )
    ).all()
    return [
        {
            "id": row.id,
            "code": row.code,
            "effective_date": row.effective_date.isoformat(),
            "available_at": row.available_at.isoformat(),
            "imported_at": row.imported_at.isoformat(),
            "source": row.source,
            "source_file": row.source_file,
            "source_hash": row.source_hash,
            "payload": dict(row.payload or {}),
        }
        for row in rows
    ]


def list_review_cases(
    db: Session,
    *,
    status: str | None = "open",
) -> list[dict[str, object]]:
    statement = select(CorporateActionReviewCase)
    if status is not None:
        statement = statement.where(CorporateActionReviewCase.status == status)
    rows = db.scalars(
        statement.order_by(CorporateActionReviewCase.created_at.desc())
    ).all()
    return [
        {
            "id": row.id,
            "record_id": row.record_id,
            "code": row.code,
            "event_key": row.event_key,
            "issue_type": row.issue_type,
            "status": row.status,
            "reason": row.reason,
            "conservative_value": float(row.conservative_value),
            "evidence": dict(row.evidence or {}),
            "resolution": dict(row.resolution or {}),
            "created_at": row.created_at.isoformat(),
            "resolved_at": (row.resolved_at.isoformat() if row.resolved_at else None),
        }
        for row in rows
    ]


def resolve_review_case(
    db: Session,
    *,
    case_id: int,
    resolution: dict[str, Any],
    operator: str,
) -> dict[str, object]:
    row = db.get(CorporateActionReviewCase, case_id)
    if row is None:
        raise ValueError(f"公司行为工单 {case_id} 不存在")
    if row.status != "open":
        raise ValueError(f"公司行为工单 {case_id} 已处于 {row.status}")
    normalized_operator = operator.strip()
    if not normalized_operator:
        raise ValueError("解决公司行为工单必须记录操作人")
    if not resolution:
        raise ValueError("解决公司行为工单必须提供官方对价或处置证据")
    row.status = "resolved"
    row.resolution = {
        **resolution,
        "operator": normalized_operator,
        "resolved_at": datetime.now(UTC).isoformat(),
    }
    row.resolved_at = datetime.now(UTC)
    db.commit()
    return next(
        item for item in list_review_cases(db, status=None) if item["id"] == case_id
    )
