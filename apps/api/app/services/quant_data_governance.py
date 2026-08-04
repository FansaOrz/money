"""量化原始快照规范化、字段来源、质量规则与 readiness 报告。"""

from __future__ import annotations

import hashlib
import json
import math
from datetime import UTC, date, datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    DataCorrection,
    DataFieldProvenance,
    DataQualityIssue,
    DataReadinessReport,
    QuantImportRun,
    QuantDataRecord,
)

SOURCE_PRIORITY = {
    "tushare": 100,
    "csindex": 90,
    "akshare": 70,
    "eastmoney": 60,
    "derived": 20,
}
RELATIVE_DIFFERENCE_LIMITS = {
    "close": 0.002,
    "total_assets": 0.01,
    "net_income": 0.02,
    "market_cap": 0.02,
}
DATASET_DATE_FIELDS = {
    "income": ("end_date", "f_ann_date", "ann_date"),
    "balancesheet": ("end_date", "f_ann_date", "ann_date"),
    "cashflow": ("end_date", "f_ann_date", "ann_date"),
    "fina_indicator": ("end_date", "ann_date", "ann_date"),
    "daily_basic": ("trade_date", "trade_date", "trade_date"),
    "adj_factor": ("trade_date", "trade_date", "trade_date"),
    "dividend": ("ex_date", "imp_ann_date", "ann_date"),
    "suspend_d": ("trade_date", "trade_date", "trade_date"),
    "stk_limit": ("trade_date", "trade_date", "trade_date"),
}


def _day(value: object) -> date | None:
    text = str(value or "")
    if len(text) >= 8 and text[:8].isdigit():
        try:
            return date(int(text[:4]), int(text[4:6]), int(text[6:8]))
        except ValueError:
            return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def _json_value(value: object) -> object:
    if value is None:
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if hasattr(value, "item"):
        return _json_value(value.item())  # type: ignore[union-attr]
    return value


def resolve_field(
    candidates: list[tuple[str, float | str | None]],
    field_name: str,
) -> tuple[float | str | None, str | None, list[str]]:
    """按字段主源优先级选值，并返回跨源差异告警。"""
    valid = [(source, value) for source, value in candidates if value is not None]
    if not valid:
        return None, None, ["全部来源缺失"]
    valid.sort(key=lambda item: SOURCE_PRIORITY.get(item[0], 0), reverse=True)
    source, selected = valid[0]
    warnings: list[str] = []
    threshold = RELATIVE_DIFFERENCE_LIMITS.get(field_name)
    if threshold is not None and isinstance(selected, (int, float)) and selected != 0:
        for other_source, other in valid[1:]:
            if isinstance(other, (int, float)):
                difference = abs(float(other) - float(selected)) / abs(float(selected))
                if difference > threshold:
                    warnings.append(
                        f"{field_name} {source}/{other_source} 差异"
                        f" {difference:.2%} > {threshold:.2%}"
                    )
    return selected, source, warnings


def import_tushare_snapshot(
    db: Session,
    root: Path,
    datasets: list[str] | None = None,
    *,
    dry_run: bool = False,
) -> dict[str, object]:
    """幂等导入规范化 PIT 记录；原始 Parquet 永不修改。"""
    import pyarrow.parquet as pq

    stock_root = root / "tushare_snapshot" / "stocks"
    wanted = datasets or list(DATASET_DATE_FIELDS)
    started_at = datetime.now(UTC)
    run = QuantImportRun(
        dataset=",".join(wanted),
        status="running",
        source_root=str(root),
        imported=0,
        skipped=0,
        invalid=0,
        detail={},
        started_at=started_at,
    )
    if not dry_run:
        db.add(run)
        db.commit()
    imported = skipped = invalid = 0
    errors: list[str] = []
    now = datetime.now(UTC)
    for dataset in wanted:
        fields = DATASET_DATE_FIELDS.get(dataset)
        if fields is None:
            errors.append(f"未知数据集 {dataset}")
            continue
        directory = stock_root / dataset
        if not directory.exists():
            errors.append(f"缺少目录 {directory}")
            continue
        effective_field, available_field, fallback_available = fields
        for path in sorted(directory.glob("*.parquet")):
            checksum = hashlib.sha256(path.read_bytes()).hexdigest()
            try:
                rows = pq.read_table(path).to_pylist()
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{path}: {exc}")
                continue
            seen: set[tuple[str, date, datetime]] = set()
            for raw in rows:
                code = str(raw.get("ts_code") or path.stem).split(".")[0]
                effective = _day(raw.get(effective_field))
                available_day = _day(
                    raw.get(available_field) or raw.get(fallback_available)
                )
                if effective is None or available_day is None:
                    invalid += 1
                    continue
                available = datetime.combine(
                    available_day, datetime.min.time(), tzinfo=UTC
                )
                natural = (code, effective, available)
                if natural in seen:
                    invalid += 1
                    continue
                seen.add(natural)
                exists = db.scalar(
                    select(QuantDataRecord.id).where(
                        QuantDataRecord.dataset == dataset,
                        QuantDataRecord.code == code,
                        QuantDataRecord.effective_date == effective,
                        QuantDataRecord.available_at == available,
                        QuantDataRecord.source == "tushare",
                    )
                )
                if exists is not None:
                    skipped += 1
                    continue
                payload = {
                    key: _json_value(value) for key, value in raw.items()
                }
                imported += 1
                if dry_run:
                    continue
                record = QuantDataRecord(
                    dataset=dataset,
                    code=code,
                    effective_date=effective,
                    available_at=available,
                    source="tushare",
                    source_file=str(path),
                    source_hash=checksum,
                    payload=payload,
                    imported_at=now,
                )
                db.add(record)
                db.flush()
                for field_name, value in payload.items():
                    db.add(
                        DataFieldProvenance(
                            record_id=record.id,
                            field_name=field_name,
                            source="tushare",
                            source_priority=SOURCE_PRIORITY["tushare"],
                            quality_status="missing" if value is None else "valid",
                            original_value=(
                                json.dumps(value, ensure_ascii=False)
                                if value is not None
                                else None
                            ),
                            normalized_value=(
                                json.dumps(value, ensure_ascii=False)
                                if value is not None
                                else None
                            ),
                        )
                    )
    if not dry_run:
        run.imported = imported
        run.skipped = skipped
        run.invalid = invalid
        run.status = "success" if not errors else "partial"
        run.detail = {"errors": errors}
        run.finished_at = datetime.now(UTC)
        db.commit()
    return {
        "datasets": wanted,
        "imported": imported,
        "skipped": skipped,
        "invalid": invalid,
        "errors": errors,
        "dry_run": dry_run,
    }


def register_corporate_action(
    db: Session,
    *,
    code: str,
    effective_date: date,
    available_at: datetime,
    payload: dict[str, object],
    source: str,
    source_file: Path,
) -> QuantDataRecord:
    """登记交易所/法定披露公司行为，并保存文件哈希与字段级来源。"""
    allowed = {
        "cash_entitlement",
        "cash_payment",
        "share_distribution",
        "rights_issue",
        "merger",
        "code_change",
        "terminal",
    }
    kind = str(payload.get("kind") or "")
    if kind not in allowed:
        raise ValueError(f"不支持的公司行为类型：{kind}")
    if not source_file.is_file():
        raise FileNotFoundError(source_file)
    normalized_code = code.split(".")[0]
    existing = db.scalar(
        select(QuantDataRecord).where(
            QuantDataRecord.dataset == "corporate_action",
            QuantDataRecord.code == normalized_code,
            QuantDataRecord.effective_date == effective_date,
            QuantDataRecord.source == source,
        )
    )
    if existing is not None:
        return existing
    checksum = hashlib.sha256(source_file.read_bytes()).hexdigest()
    record = QuantDataRecord(
        dataset="corporate_action",
        code=normalized_code,
        effective_date=effective_date,
        available_at=available_at,
        source=source,
        source_file=str(source_file),
        source_hash=checksum,
        payload={
            key: _json_value(value) for key, value in payload.items()
        },
        imported_at=datetime.now(UTC),
    )
    db.add(record)
    db.flush()
    for field_name, value in record.payload.items():
        encoded = (
            json.dumps(value, ensure_ascii=False)
            if value is not None
            else None
        )
        db.add(
            DataFieldProvenance(
                record_id=record.id,
                field_name=field_name,
                source=source,
                source_priority=SOURCE_PRIORITY.get(source, 100),
                quality_status="missing" if value is None else "valid",
                original_value=encoded,
                normalized_value=encoded,
            )
        )
    db.commit()
    db.refresh(record)
    return record


def save_readiness(
    db: Session,
    strategy_name: str,
    signal_date: date,
    rows: dict[str, dict[str, object]],
) -> dict[str, object]:
    """持久化逐股逐字段门禁，旧报告按自然键覆盖。"""
    generated_at = datetime.now(UTC)
    ready_count = 0
    for code, field_status in rows.items():
        ready = all(bool(value) for value in field_status.values())
        ready_count += int(ready)
        existing = db.scalar(
            select(DataReadinessReport).where(
                DataReadinessReport.strategy_name == strategy_name,
                DataReadinessReport.signal_date == signal_date,
                DataReadinessReport.code == code,
            )
        )
        row = existing or DataReadinessReport(
            strategy_name=strategy_name,
            signal_date=signal_date,
            code=code,
            ready=ready,
            field_status=field_status,
            generated_at=generated_at,
        )
        row.ready = ready
        row.field_status = field_status
        row.generated_at = generated_at
        db.add(row)
    db.commit()
    return {
        "total": len(rows),
        "ready": ready_count,
        "coverage": ready_count / len(rows) if rows else 0.0,
    }


def record_quality_issue(
    db: Session,
    *,
    dataset: str,
    rule: str,
    detail: str,
    severity: str = "warning",
    code: str | None = None,
    field_name: str | None = None,
    original_value: object = None,
    source: str | None = None,
) -> DataQualityIssue:
    issue = DataQualityIssue(
        dataset=dataset,
        code=code,
        field_name=field_name,
        severity=severity,
        rule=rule,
        detail=detail,
        original_value=repr(original_value),
        source=source,
        status="open",
        detected_at=datetime.now(UTC),
    )
    db.add(issue)
    db.flush()
    return issue


def apply_correction(
    db: Session,
    issue: DataQualityIssue,
    *,
    corrected_value: object,
    correction_rule: str,
    actor: str,
) -> DataCorrection:
    """记录修正但绝不改写原始 payload；规范化消费者读取修正日志。"""
    correction = DataCorrection(
        issue_id=issue.id,
        original_value=issue.original_value,
        corrected_value=json.dumps(corrected_value, ensure_ascii=False),
        correction_rule=correction_rule,
        source=issue.source,
        actor=actor,
        corrected_at=datetime.now(UTC),
    )
    issue.status = "resolved"
    issue.resolved_at = datetime.now(UTC)
    db.add(correction)
    db.commit()
    db.refresh(correction)
    return correction
