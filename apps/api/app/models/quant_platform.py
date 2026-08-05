"""量化平台的数据治理、任务、审计与模拟 OMS/RMS 持久化模型。"""

from __future__ import annotations

from datetime import UTC, date, datetime
import hashlib
import json

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    DDL,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    event,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class QuantDataRecord(Base):
    """不可变原始表到规范化层的 PIT 记录。"""

    __tablename__ = "quant_data_records"
    __table_args__ = (
        UniqueConstraint(
            "dataset",
            "code",
            "effective_date",
            "available_at",
            "source",
            name="uq_quant_data_record_natural",
        ),
        Index("ix_quant_data_record_lookup", "dataset", "code", "effective_date"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    dataset: Mapped[str] = mapped_column(String(40), nullable=False)
    code: Mapped[str] = mapped_column(String(20), nullable=False)
    effective_date: Mapped[date] = mapped_column(Date, nullable=False)
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    source: Mapped[str] = mapped_column(String(50), nullable=False)
    source_file: Mapped[str] = mapped_column(String(500), nullable=False)
    source_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)
    imported_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )


class DataFieldProvenance(Base):
    """字段级主源、回退源与质量状态。"""

    __tablename__ = "data_field_provenance"
    __table_args__ = (
        UniqueConstraint(
            "record_id", "field_name", name="uq_data_field_provenance_record_field"
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    record_id: Mapped[int] = mapped_column(
        ForeignKey("quant_data_records.id"), nullable=False, index=True
    )
    field_name: Mapped[str] = mapped_column(String(80), nullable=False)
    source: Mapped[str] = mapped_column(String(50), nullable=False)
    source_priority: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    quality_status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="valid"
    )
    original_value: Mapped[str | None] = mapped_column(Text)
    normalized_value: Mapped[str | None] = mapped_column(Text)


class DataQualityIssue(Base):
    __tablename__ = "data_quality_issues"
    __table_args__ = (Index("ix_data_quality_open", "status", "dataset", "code"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    dataset: Mapped[str] = mapped_column(String(40), nullable=False)
    code: Mapped[str | None] = mapped_column(String(20))
    field_name: Mapped[str | None] = mapped_column(String(80))
    severity: Mapped[str] = mapped_column(String(20), nullable=False)
    rule: Mapped[str] = mapped_column(String(100), nullable=False)
    detail: Mapped[str] = mapped_column(Text, nullable=False)
    original_value: Mapped[str | None] = mapped_column(Text)
    source: Mapped[str | None] = mapped_column(String(50))
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="open")
    detected_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class FactorHealthReport(Base):
    """每个信号日/因子的分布、漂移和正式阻断证据。"""

    __tablename__ = "factor_health_reports"
    __table_args__ = (
        UniqueConstraint(
            "strategy_version_id",
            "signal_date",
            "factor_name",
            name="uq_factor_health_version_date_factor",
        ),
        Index("ix_factor_health_status_date", "status", "signal_date"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    strategy_version_id: Mapped[int | None] = mapped_column(
        ForeignKey("strategy_versions.id"), index=True
    )
    signal_date: Mapped[date] = mapped_column(Date, nullable=False)
    factor_name: Mapped[str] = mapped_column(String(100), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    unit: Mapped[str] = mapped_column(String(40), nullable=False)
    direction: Mapped[int] = mapped_column(Integer, nullable=False)
    statistics: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    reasons: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )


class FactorMonitorSnapshot(Base):
    """滚动 IC、拥挤、容量和结构突变的动作快照。"""

    __tablename__ = "factor_monitor_snapshots"
    __table_args__ = (
        UniqueConstraint(
            "strategy_version_id",
            "as_of",
            "factor_name",
            name="uq_factor_monitor_version_date_factor",
        ),
        Index("ix_factor_monitor_action_date", "action", "as_of"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    strategy_version_id: Mapped[int | None] = mapped_column(
        ForeignKey("strategy_versions.id"), index=True
    )
    as_of: Mapped[date] = mapped_column(Date, nullable=False)
    factor_name: Mapped[str] = mapped_column(String(100), nullable=False)
    action: Mapped[str] = mapped_column(String(30), nullable=False)
    metrics: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    reasons: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    policy_version: Mapped[str] = mapped_column(
        String(50), nullable=False, default="FACTOR_MONITOR_V1"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )


class ResearchExperiment(Base):
    """运行前预注册的研究假设；完成、失败和废弃记录均不可删除。"""

    __tablename__ = "research_experiments"

    id: Mapped[int] = mapped_column(primary_key=True)
    experiment_key: Mapped[str] = mapped_column(
        String(100), nullable=False, unique=True
    )
    hypothesis: Mapped[str] = mapped_column(Text, nullable=False)
    parameter_space: Mapped[dict] = mapped_column(JSON, nullable=False)
    target_metrics: Mapped[list] = mapped_column(JSON, nullable=False)
    data_scope: Mapped[dict] = mapped_column(JSON, nullable=False)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="registered"
    )
    registered_by: Mapped[str] = mapped_column(String(100), nullable=False)
    registered_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    result_summary: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    registration_sha256: Mapped[str] = mapped_column(String(64), nullable=False)


class ResearchTrialAttempt(Base):
    """每个因子/权重/过滤器/参数尝试，包括失败尝试。"""

    __tablename__ = "research_trial_attempts"
    __table_args__ = (
        UniqueConstraint(
            "experiment_id", "trial_key", name="uq_research_trial_experiment_key"
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    experiment_id: Mapped[int] = mapped_column(
        ForeignKey("research_experiments.id"), nullable=False, index=True
    )
    trial_key: Mapped[str] = mapped_column(String(120), nullable=False)
    factor_spec: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    parameters: Mapped[dict] = mapped_column(JSON, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    metrics: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    score_series: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    error: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    finished_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    result_sha256: Mapped[str] = mapped_column(String(64), nullable=False)


class HoldoutConsumption(Base):
    """完全留出区间的永久查看登记；同一区间一经查看不再恢复 pristine。"""

    __tablename__ = "holdout_consumptions"
    __table_args__ = (
        UniqueConstraint(
            "experiment_id",
            "interval_start",
            "interval_end",
            name="uq_holdout_consumption_experiment_interval",
        ),
        Index(
            "ix_holdout_consumption_interval",
            "interval_start",
            "interval_end",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    experiment_id: Mapped[int] = mapped_column(
        ForeignKey("research_experiments.id"), nullable=False, index=True
    )
    strategy_version_id: Mapped[int | None] = mapped_column(
        ForeignKey("strategy_versions.id"), index=True
    )
    interval_start: Mapped[date] = mapped_column(Date, nullable=False)
    interval_end: Mapped[date] = mapped_column(Date, nullable=False)
    purpose: Mapped[str] = mapped_column(String(100), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    result_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    consumed_by: Mapped[str] = mapped_column(String(100), nullable=False)
    consumed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )


@event.listens_for(ResearchExperiment, "before_delete")
@event.listens_for(ResearchTrialAttempt, "before_delete")
def _prevent_research_history_delete(_mapper, _connection, _target) -> None:
    raise ValueError("研究实验与失败尝试是不可删除审计记录")


class DataCorrection(Base):
    __tablename__ = "data_corrections"

    id: Mapped[int] = mapped_column(primary_key=True)
    issue_id: Mapped[int] = mapped_column(
        ForeignKey("data_quality_issues.id"), nullable=False, index=True
    )
    original_value: Mapped[str | None] = mapped_column(Text)
    corrected_value: Mapped[str | None] = mapped_column(Text)
    correction_rule: Mapped[str] = mapped_column(String(100), nullable=False)
    source: Mapped[str | None] = mapped_column(String(50))
    actor: Mapped[str] = mapped_column(String(100), nullable=False)
    affected_strategy_versions: Mapped[list] = mapped_column(
        JSON, nullable=False, default=list
    )
    evidence_sha256: Mapped[str] = mapped_column(
        String(64), nullable=False, default=""
    )
    corrected_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )


class QuantImportRun(Base):
    __tablename__ = "quant_import_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    dataset: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    source_root: Mapped[str] = mapped_column(String(500), nullable=False)
    imported: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    skipped: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    invalid: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    detail: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class DataSourceSLAState(Base):
    """必需量化数据集的数据供应政策、SLA 与最近运行状态。"""

    __tablename__ = "data_source_sla_states"

    dataset: Mapped[str] = mapped_column(String(40), primary_key=True)
    required: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    primary_source: Mapped[str] = mapped_column(String(80), nullable=False)
    fallback_source: Mapped[str | None] = mapped_column(String(80))
    license_class: Mapped[str] = mapped_column(String(30), nullable=False)
    frequency_minutes: Mapped[int] = mapped_column(Integer, nullable=False)
    max_latency_minutes: Mapped[int] = mapped_column(Integer, nullable=False)
    rate_limit: Mapped[str | None] = mapped_column(String(100))
    owner: Mapped[str] = mapped_column(String(100), nullable=False)
    failure_mode: Mapped[str] = mapped_column(String(50), nullable=False)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="never_run"
    )
    active_source: Mapped[str | None] = mapped_column(String(80))
    last_attempted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    last_success_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    data_date: Mapped[date | None] = mapped_column(Date)
    schema_hash: Mapped[str | None] = mapped_column(String(64))
    row_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    consecutive_failures: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0
    )
    degraded: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    escalation_level: Mapped[str] = mapped_column(
        String(20), nullable=False, default="none"
    )
    error: Mapped[str | None] = mapped_column(Text)
    detail: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)


class DataSourceReconciliation(Base):
    """同一字段多来源比较、最终选值与升级决定。"""

    __tablename__ = "data_source_reconciliations"
    __table_args__ = (
        Index(
            "ix_source_reconciliation_status",
            "status",
            "dataset",
            "effective_date",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    dataset: Mapped[str] = mapped_column(String(40), nullable=False)
    code: Mapped[str] = mapped_column(String(20), nullable=False)
    effective_date: Mapped[date] = mapped_column(Date, nullable=False)
    field_name: Mapped[str] = mapped_column(String(80), nullable=False)
    candidates: Mapped[list] = mapped_column(JSON, nullable=False)
    relative_difference: Mapped[float | None] = mapped_column(Numeric(18, 8))
    threshold: Mapped[float | None] = mapped_column(Numeric(18, 8))
    status: Mapped[str] = mapped_column(String(30), nullable=False)
    selected_source: Mapped[str | None] = mapped_column(String(80))
    selected_value: Mapped[str | None] = mapped_column(Text)
    rationale: Mapped[str] = mapped_column(Text, nullable=False)
    safe_action: Mapped[str] = mapped_column(String(50), nullable=False)
    checked_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    resolved_by: Mapped[str | None] = mapped_column(String(100))
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class DataFileManifestEntry(Base):
    __tablename__ = "data_file_manifest_entries"
    __table_args__ = (
        UniqueConstraint(
            "snapshot_sha256",
            "relative_path",
            name="uq_data_file_manifest_snapshot_path",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    snapshot_sha256: Mapped[str] = mapped_column(
        String(64), nullable=False, index=True
    )
    relative_path: Mapped[str] = mapped_column(String(700), nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    file_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    frozen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )


class DataFileAccessLog(Base):
    __tablename__ = "data_file_access_logs"
    __table_args__ = (
        Index(
            "ix_data_file_access_snapshot_version",
            "snapshot_sha256",
            "strategy_version_id",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    snapshot_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    strategy_version_id: Mapped[int | None] = mapped_column(
        ForeignKey("strategy_versions.id")
    )
    relative_path: Mapped[str] = mapped_column(String(700), nullable=False)
    observed_size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    observed_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(30), nullable=False)
    detail: Mapped[str] = mapped_column(Text, nullable=False)
    accessed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )


class DataReadinessReport(Base):
    __tablename__ = "data_readiness_reports"
    __table_args__ = (
        UniqueConstraint(
            "strategy_name",
            "strategy_version_id",
            "signal_date",
            "code",
            name="uq_data_readiness_strategy_day_code",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    strategy_name: Mapped[str] = mapped_column(String(100), nullable=False)
    strategy_version_id: Mapped[int | None] = mapped_column(
        ForeignKey("strategy_versions.id"), index=True
    )
    signal_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    code: Mapped[str] = mapped_column(String(20), nullable=False)
    ready: Mapped[bool] = mapped_column(Boolean, nullable=False)
    field_status: Mapped[dict] = mapped_column(JSON, nullable=False)
    data_snapshot_sha256: Mapped[str] = mapped_column(
        String(64), nullable=False, default=""
    )
    report_sha256: Mapped[str] = mapped_column(
        String(64), nullable=False, default=""
    )
    generated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )


class PersistentJob(Base):
    __tablename__ = "persistent_jobs"
    __table_args__ = (
        UniqueConstraint("job_name", "scheduled_for", name="uq_job_schedule"),
        Index("ix_persistent_job_claim", "status", "scheduled_for", "locked_until"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    job_name: Mapped[str] = mapped_column(String(80), nullable=False)
    scheduled_for: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="queued")
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    locked_by: Mapped[str | None] = mapped_column(String(100))
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    depends_on: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    checkpoint: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    correlation_id: Mapped[str] = mapped_column(String(64), nullable=False)
    error: Mapped[str | None] = mapped_column(Text)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AuditLog(Base):
    __tablename__ = "audit_logs"
    __table_args__ = (Index("ix_audit_log_time_actor", "created_at", "actor"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    actor: Mapped[str] = mapped_column(String(100), nullable=False)
    action: Mapped[str] = mapped_column(String(100), nullable=False)
    resource_type: Mapped[str] = mapped_column(String(60), nullable=False)
    resource_id: Mapped[str | None] = mapped_column(String(100))
    correlation_id: Mapped[str | None] = mapped_column(String(64))
    detail: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    previous_hash: Mapped[str] = mapped_column(
        String(64), nullable=False, default="0" * 64
    )
    entry_hash: Mapped[str] = mapped_column(
        String(64), nullable=False, unique=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )


event.listen(
    AuditLog.__table__,
    "after_create",
    DDL(
        """
        CREATE TRIGGER IF NOT EXISTS prevent_audit_update
        BEFORE UPDATE ON audit_logs
        BEGIN SELECT RAISE(ABORT, 'audit log is immutable'); END
        """
    ).execute_if(dialect="sqlite"),
)
event.listen(
    AuditLog.__table__,
    "after_create",
    DDL(
        """
        CREATE TRIGGER IF NOT EXISTS prevent_audit_delete
        BEFORE DELETE ON audit_logs
        BEGIN SELECT RAISE(ABORT, 'audit log is immutable'); END
        """
    ).execute_if(dialect="sqlite"),
)


@event.listens_for(AuditLog, "before_insert")
def _hash_audit_entry(_mapper, connection, target: AuditLog) -> None:
    if connection.dialect.name == "postgresql":
        connection.exec_driver_sql(
            "SELECT pg_advisory_xact_lock(741852963)"
        )
    previous = connection.execute(
        AuditLog.__table__.select()
        .with_only_columns(AuditLog.entry_hash)
        .order_by(AuditLog.id.desc())
        .limit(1)
    ).scalar()
    target.previous_hash = previous or "0" * 64
    payload = {
        "previous_hash": target.previous_hash,
        "actor": target.actor,
        "action": target.action,
        "resource_type": target.resource_type,
        "resource_id": target.resource_id,
        "correlation_id": target.correlation_id,
        "detail": target.detail,
        "created_at": (
            target.created_at.astimezone(UTC).replace(tzinfo=None)
            if target.created_at.tzinfo
            else target.created_at
        ).isoformat(timespec="microseconds"),
    }
    target.entry_hash = hashlib.sha256(
        json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode()
    ).hexdigest()


@event.listens_for(AuditLog, "before_update")
@event.listens_for(AuditLog, "before_delete")
def _prevent_audit_mutation(_mapper, _connection, _target) -> None:
    raise ValueError("审计哈希链不可修改或删除")


class StrategyTransition(Base):
    __tablename__ = "strategy_transitions"

    id: Mapped[int] = mapped_column(primary_key=True)
    strategy_version_id: Mapped[int] = mapped_column(
        ForeignKey("strategy_versions.id"), nullable=False, index=True
    )
    from_status: Mapped[str] = mapped_column(String(30), nullable=False)
    to_status: Mapped[str] = mapped_column(String(30), nullable=False)
    gates: Mapped[dict] = mapped_column(JSON, nullable=False)
    approved: Mapped[bool] = mapped_column(Boolean, nullable=False)
    actor: Mapped[str] = mapped_column(String(100), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )


class BrokerOrder(Base):
    """统一 OMS 订单；默认 adapter=simulated，不含任何真实下单副作用。"""

    __tablename__ = "broker_orders"
    __table_args__ = (
        UniqueConstraint("client_order_id", name="uq_broker_order_client_id"),
        CheckConstraint("quantity > 0", name="ck_broker_order_quantity_positive"),
        CheckConstraint(
            "reference_price IS NULL OR reference_price > 0",
            name="ck_broker_order_reference_price_positive",
        ),
        CheckConstraint("side IN ('buy', 'sell')", name="ck_broker_order_side"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    client_order_id: Mapped[str] = mapped_column(String(100), nullable=False)
    account: Mapped[str] = mapped_column(String(100), nullable=False)
    code: Mapped[str] = mapped_column(String(20), nullable=False)
    side: Mapped[str] = mapped_column(String(10), nullable=False)
    order_type: Mapped[str] = mapped_column(String(20), nullable=False)
    quantity: Mapped[float] = mapped_column(Numeric(20, 6), nullable=False)
    limit_price: Mapped[float | None] = mapped_column(Numeric(20, 6))
    reference_price: Mapped[float | None] = mapped_column(Numeric(20, 6))
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    adapter: Mapped[str] = mapped_column(
        String(40), nullable=False, default="simulated"
    )
    broker_order_id: Mapped[str | None] = mapped_column(String(100), index=True)
    broker_batch_id: Mapped[str | None] = mapped_column(String(100))
    strategy_version_id: Mapped[int | None] = mapped_column(
        ForeignKey("strategy_versions.id")
    )
    risk_result: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )


class BrokerFill(Base):
    __tablename__ = "broker_fills"
    __table_args__ = (
        UniqueConstraint(
            "adapter",
            "account",
            "trade_date",
            "external_fill_id",
            name="uq_broker_fill_external",
        ),
        CheckConstraint("quantity <> 0", name="ck_broker_fill_quantity_nonzero"),
        CheckConstraint("price > 0", name="ck_broker_fill_price_positive"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(
        ForeignKey("broker_orders.id"), nullable=False, index=True
    )
    adapter: Mapped[str] = mapped_column(String(40), nullable=False)
    account: Mapped[str] = mapped_column(
        String(100), nullable=False, default="legacy"
    )
    trade_date: Mapped[date] = mapped_column(Date, nullable=False, default=date.today)
    external_fill_id: Mapped[str] = mapped_column(String(100), nullable=False)
    event_type: Mapped[str] = mapped_column(
        String(20), nullable=False, default="fill"
    )
    original_external_fill_id: Mapped[str | None] = mapped_column(String(100))
    quantity: Mapped[float] = mapped_column(Numeric(20, 6), nullable=False)
    price: Mapped[float] = mapped_column(Numeric(20, 6), nullable=False)
    fee: Mapped[float] = mapped_column(Numeric(20, 6), nullable=False)
    fee_rule_version: Mapped[str] = mapped_column(
        String(100), nullable=False, default="BROKER_REPORTED"
    )
    fee_breakdown: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    lot_consumption: Mapped[list[dict]] = mapped_column(
        JSON, nullable=False, default=list
    )
    arrival_price: Mapped[float | None] = mapped_column(Numeric(20, 6))
    open_price: Mapped[float | None] = mapped_column(Numeric(20, 6))
    decision_price: Mapped[float | None] = mapped_column(Numeric(20, 6))
    market_vwap: Mapped[float | None] = mapped_column(Numeric(20, 6))
    close_price: Mapped[float | None] = mapped_column(Numeric(20, 6))
    participation_rate: Mapped[float | None] = mapped_column(Numeric(12, 8))
    implementation_shortfall: Mapped[float | None] = mapped_column(Numeric(12, 8))
    recent_volatility: Mapped[float | None] = mapped_column(Numeric(12, 8))
    liquidity_adv: Mapped[float | None] = mapped_column(Numeric(24, 6))
    execution_session: Mapped[str | None] = mapped_column(String(20))
    slippage_model_version: Mapped[str] = mapped_column(
        String(100), nullable=False, default="BROKER_REPORTED"
    )
    filled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class BrokerAccountLedger(Base):
    __tablename__ = "broker_account_ledgers"
    __table_args__ = (
        CheckConstraint(
            "cash >= 0", name="ck_broker_ledger_cash_nonnegative"
        ),
    )

    account: Mapped[str] = mapped_column(String(100), primary_key=True)
    adapter: Mapped[str] = mapped_column(
        String(40), nullable=False, default="simulated"
    )
    cash: Mapped[float] = mapped_column(Numeric(20, 2), nullable=False)
    positions: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    position_lots: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    reconciliation_status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="not_checked"
    )
    last_reconciled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    row_version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1
    )

    __mapper_args__ = {"version_id_col": row_version}


class RiskControlState(Base):
    __tablename__ = "risk_control_state"

    account: Mapped[str] = mapped_column(String(100), primary_key=True)
    kill_switch: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    max_order_value: Mapped[float] = mapped_column(
        Numeric(20, 2), nullable=False, default=100000
    )
    max_daily_turnover: Mapped[float] = mapped_column(
        Numeric(20, 2), nullable=False, default=500000
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )


class BrokerOrderEvent(Base):
    """券商订单不可变事件流；当前状态由事件重放归约。"""

    __tablename__ = "broker_order_events"
    __table_args__ = (
        UniqueConstraint("order_id", "sequence", name="uq_order_event_sequence"),
        UniqueConstraint(
            "adapter",
            "external_event_id",
            name="uq_order_event_external",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(
        ForeignKey("broker_orders.id"), nullable=False, index=True
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    broker_sequence: Mapped[int | None] = mapped_column(Integer)
    event_type: Mapped[str] = mapped_column(String(30), nullable=False)
    adapter: Mapped[str] = mapped_column(String(40), nullable=False)
    external_event_id: Mapped[str] = mapped_column(String(120), nullable=False)
    broker_order_id: Mapped[str | None] = mapped_column(String(100))
    broker_batch_id: Mapped[str | None] = mapped_column(String(100))
    broker_fill_id: Mapped[str | None] = mapped_column(String(100))
    payload: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )


class ReconciliationRun(Base):
    __tablename__ = "reconciliation_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    account: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    adapter: Mapped[str] = mapped_column(String(40), nullable=False)
    trade_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    broker_snapshot_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    broker_snapshot: Mapped[dict] = mapped_column(JSON, nullable=False)
    local_snapshot: Mapped[dict] = mapped_column(JSON, nullable=False)
    tolerance: Mapped[dict] = mapped_column(JSON, nullable=False)
    categories: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    responsible_owner: Mapped[str | None] = mapped_column(String(100))
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    completed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )


class ReconciliationBreak(Base):
    __tablename__ = "reconciliation_breaks"

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(
        ForeignKey("reconciliation_runs.id"), nullable=False, index=True
    )
    break_type: Mapped[str] = mapped_column(String(40), nullable=False)
    code: Mapped[str | None] = mapped_column(String(20))
    expected: Mapped[dict] = mapped_column(JSON, nullable=False)
    actual: Mapped[dict] = mapped_column(JSON, nullable=False)
    difference: Mapped[dict] = mapped_column(JSON, nullable=False)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="open"
    )
    owner: Mapped[str | None] = mapped_column(String(100))
    resolution: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class StrategyControlState(Base):
    __tablename__ = "strategy_control_states"
    __table_args__ = (
        UniqueConstraint(
            "account",
            "strategy_version_id",
            name="uq_strategy_control_account_version",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    account: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    strategy_version_id: Mapped[int | None] = mapped_column(
        ForeignKey("strategy_versions.id")
    )
    mode: Mapped[str] = mapped_column(
        String(20), nullable=False, default="running"
    )
    reason: Mapped[str] = mapped_column(Text, nullable=False, default="")
    operator: Mapped[str] = mapped_column(String(100), nullable=False)
    approver: Mapped[str | None] = mapped_column(String(100))
    scope: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )


class KillSwitchDrill(Base):
    __tablename__ = "kill_switch_drills"

    id: Mapped[int] = mapped_column(primary_key=True)
    account: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    triggered_by: Mapped[str] = mapped_column(String(100), nullable=False)
    approved_by: Mapped[str] = mapped_column(String(100), nullable=False)
    policy: Mapped[dict] = mapped_column(JSON, nullable=False)
    cancelled_orders: Mapped[list] = mapped_column(JSON, nullable=False)
    failed_orders: Mapped[list] = mapped_column(JSON, nullable=False)
    elapsed_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    sla_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    passed: Mapped[bool] = mapped_column(Boolean, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )


class OperationalMetric(Base):
    __tablename__ = "operational_metrics"
    __table_args__ = (
        Index("ix_operational_metric_name_time", "metric_name", "observed_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    metric_name: Mapped[str] = mapped_column(String(100), nullable=False)
    value: Mapped[float] = mapped_column(Numeric(24, 6), nullable=False)
    unit: Mapped[str] = mapped_column(String(30), nullable=False)
    labels: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    budget: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    observed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )


class ExternalAlert(Base):
    __tablename__ = "external_alerts"
    __table_args__ = (
        UniqueConstraint("dedup_key", "status", name="uq_alert_dedup_status"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    dedup_key: Mapped[str] = mapped_column(String(160), nullable=False)
    severity: Mapped[str] = mapped_column(String(20), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)
    channel: Mapped[str] = mapped_column(String(40), nullable=False)
    delivery_attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0
    )
    last_delivery_status: Mapped[str] = mapped_column(String(30), nullable=False)
    acknowledged_by: Mapped[str | None] = mapped_column(String(100))
    escalation_level: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0
    )
    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    recovered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ApiIdentity(Base):
    __tablename__ = "api_identities"

    id: Mapped[int] = mapped_column(primary_key=True)
    identity_key: Mapped[str] = mapped_column(
        String(100), nullable=False, unique=True
    )
    identity_type: Mapped[str] = mapped_column(String(20), nullable=False)
    secret_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    scopes: Mapped[list] = mapped_column(JSON, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    mfa_required: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    rotated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class CorporateActionReviewCase(Base):
    """公司行为未知对价、冲突、零碎权益和更正的人工审计工单。"""

    __tablename__ = "corporate_action_review_cases"
    __table_args__ = (
        UniqueConstraint(
            "code",
            "event_key",
            "issue_type",
            name="uq_corporate_action_review_issue",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    record_id: Mapped[int | None] = mapped_column(
        ForeignKey("quant_data_records.id"), index=True
    )
    code: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    event_key: Mapped[str] = mapped_column(String(160), nullable=False)
    issue_type: Mapped[str] = mapped_column(String(50), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="open")
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    conservative_value: Mapped[float] = mapped_column(
        Numeric(20, 6), nullable=False, default=0
    )
    evidence: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    resolution: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
