"""关键字段跨源比较、异常主源降级和人工复核阻断。"""

from __future__ import annotations

from datetime import UTC, date, datetime
import json
from typing import Iterable

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import DataSourceReconciliation
from app.services.quant_data_governance import (
    RELATIVE_DIFFERENCE_LIMITS,
    SOURCE_PRIORITY,
    record_quality_issue,
)


class SourceConflictError(RuntimeError):
    """来源差异超过政策阈值，正式消费必须停止。"""


PLAUSIBLE_RANGES = {
    "close": (0.01, 100_000.0),
    "roe": (-10.0, 10.0),
    "total_assets": (0.0, 1e16),
    "net_income": (-1e15, 1e15),
    "market_cap": (0.0, 1e17),
    "pe_ttm": (-10_000.0, 10_000.0),
    "pb": (-1000.0, 1000.0),
}


def _numeric(value: object) -> float | None:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _plausible(field_name: str, value: object) -> bool:
    bounds = PLAUSIBLE_RANGES.get(field_name)
    if bounds is None:
        return value not in (None, "")
    numeric = _numeric(value)
    return numeric is not None and bounds[0] <= numeric <= bounds[1]


def reconcile_field(
    db: Session,
    *,
    dataset: str,
    code: str,
    effective_date: date,
    field_name: str,
    candidates: Iterable[tuple[str, object]],
    threshold: float | None = None,
    safe_action: str = "halt_new_orders",
    commit: bool = True,
) -> DataSourceReconciliation:
    """执行一次版本化选值；冲突不返回伪完整值。"""
    valid = [
        (source, value)
        for source, value in candidates
        if value not in (None, "")
    ]
    valid.sort(
        key=lambda item: SOURCE_PRIORITY.get(item[0].split(":")[0], 0),
        reverse=True,
    )
    selected_source: str | None = None
    selected_value: object = None
    relative_difference: float | None = None
    policy_threshold = (
        threshold
        if threshold is not None
        else RELATIVE_DIFFERENCE_LIMITS.get(field_name)
    )
    status = "missing"
    rationale = "全部来源缺失"
    if valid:
        primary_source, primary_value = valid[0]
        if not _plausible(field_name, primary_value):
            fallback = next(
                (
                    (source, value)
                    for source, value in valid[1:]
                    if _plausible(field_name, value)
                ),
                None,
            )
            if fallback is not None:
                selected_source, selected_value = fallback
                status = "degraded"
                rationale = (
                    f"主源 {primary_source} 值不符合经济范围，"
                    f"降级选择 {selected_source}"
                )
            else:
                status = "blocked"
                rationale = "主源异常且无可用备用源"
        else:
            selected_source, selected_value = primary_source, primary_value
            comparable = next(
                (
                    (source, value)
                    for source, value in valid[1:]
                    if _plausible(field_name, value)
                ),
                None,
            )
            if comparable is not None:
                other_source, other_value = comparable
                primary_number = _numeric(primary_value)
                other_number = _numeric(other_value)
                if (
                    primary_number is not None
                    and other_number is not None
                    and primary_number != 0
                ):
                    relative_difference = abs(
                        primary_number - other_number
                    ) / abs(primary_number)
                    if (
                        policy_threshold is not None
                        and relative_difference > policy_threshold
                    ):
                        status = "review_required"
                        selected_source = None
                        selected_value = None
                        rationale = (
                            f"{primary_source}/{other_source} 差异"
                            f" {relative_difference:.4%} 超过"
                            f" {policy_threshold:.4%}"
                        )
                    else:
                        status = "matched"
                        rationale = (
                            f"主源优先且与 {other_source} 差异在阈值内"
                        )
                elif str(primary_value) != str(other_value):
                    status = "review_required"
                    selected_source = None
                    selected_value = None
                    rationale = (
                        f"{primary_source}/{other_source} 分类值不一致"
                    )
                else:
                    status = "matched"
                    rationale = "多来源值一致"
            else:
                status = "single_source"
                rationale = f"仅 {primary_source} 有可用值"
    decision = DataSourceReconciliation(
        dataset=dataset,
        code=code,
        effective_date=effective_date,
        field_name=field_name,
        candidates=[
            {"source": source, "value": value} for source, value in valid
        ],
        relative_difference=relative_difference,
        threshold=policy_threshold,
        status=status,
        selected_source=selected_source,
        selected_value=(
            json.dumps(selected_value, ensure_ascii=False)
            if selected_source is not None
            else None
        ),
        rationale=rationale,
        safe_action=safe_action,
        checked_at=datetime.now(UTC),
    )
    db.add(decision)
    db.flush()
    if status in {"degraded", "blocked", "review_required", "missing"}:
        record_quality_issue(
            db,
            dataset=dataset,
            code=code,
            field_name=field_name,
            rule=f"cross_source_{status}",
            detail=rationale,
            severity="error" if status != "degraded" else "warning",
            original_value=decision.candidates,
            source="source_reconciliation",
        )
    if commit:
        db.commit()
        db.refresh(decision)
    return decision


def selected_value(decision: DataSourceReconciliation) -> object:
    if decision.status in {"blocked", "review_required", "missing"}:
        raise SourceConflictError(
            f"{decision.dataset}/{decision.code}/{decision.field_name}: "
            f"{decision.rationale}；安全动作={decision.safe_action}"
        )
    if decision.selected_value is None:
        raise SourceConflictError("选值决策缺少 selected_value")
    return json.loads(decision.selected_value)


def resolve_review(
    db: Session,
    decision: DataSourceReconciliation,
    *,
    selected_source: str,
    selected_value_override: object,
    actor: str,
    rationale: str,
) -> DataSourceReconciliation:
    allowed = {
        str(item["source"]) for item in decision.candidates
    }
    if selected_source not in allowed:
        raise ValueError("人工选源必须来自已记录候选来源")
    decision.status = "resolved"
    decision.selected_source = selected_source
    decision.selected_value = json.dumps(
        selected_value_override, ensure_ascii=False
    )
    decision.rationale = rationale
    decision.resolved_by = actor
    decision.resolved_at = datetime.now(UTC)
    db.commit()
    db.refresh(decision)
    return decision


def reconciliation_gate(
    db: Session,
    *,
    as_of: date,
) -> dict[str, object]:
    """每个字段只取最近一次决定；未解决冲突阻断新信号。"""
    latest_ids = (
        select(func.max(DataSourceReconciliation.id))
        .where(DataSourceReconciliation.effective_date <= as_of)
        .group_by(
            DataSourceReconciliation.dataset,
            DataSourceReconciliation.code,
            DataSourceReconciliation.field_name,
        )
        .scalar_subquery()
    )
    rows = db.scalars(
        select(DataSourceReconciliation).where(
            DataSourceReconciliation.id.in_(latest_ids)
        )
    ).all()
    blocking_statuses = {"blocked", "review_required", "missing"}
    blocking = [row for row in rows if row.status in blocking_statuses]
    degraded = [row for row in rows if row.status == "degraded"]
    by_code: dict[str, list[str]] = {}
    for row in blocking:
        by_code.setdefault(row.code, []).append(
            f"{row.dataset}.{row.field_name}:{row.status}"
        )
    return {
        "ready": not blocking,
        "checked": len(rows),
        "blocking": len(blocking),
        "degraded": len(degraded),
        "blocking_by_code": by_code,
        "safe_action": "halt_new_orders" if blocking else "normal",
    }
