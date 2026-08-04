"""量化平台的数据治理、任务、审计与模拟 OMS/RMS 持久化模型。"""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
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
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    source: Mapped[str] = mapped_column(String(50), nullable=False)
    source_file: Mapped[str] = mapped_column(String(500), nullable=False)
    source_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)
    imported_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


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
    __table_args__ = (
        Index("ix_data_quality_open", "status", "dataset", "code"),
    )

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
    detected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


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
    corrected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


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
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class DataReadinessReport(Base):
    __tablename__ = "data_readiness_reports"
    __table_args__ = (
        UniqueConstraint(
            "strategy_name",
            "signal_date",
            "code",
            name="uq_data_readiness_strategy_day_code",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    strategy_name: Mapped[str] = mapped_column(String(100), nullable=False)
    signal_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    code: Mapped[str] = mapped_column(String(20), nullable=False)
    ready: Mapped[bool] = mapped_column(Boolean, nullable=False)
    field_status: Mapped[dict] = mapped_column(JSON, nullable=False)
    generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class PersistentJob(Base):
    __tablename__ = "persistent_jobs"
    __table_args__ = (
        UniqueConstraint("job_name", "scheduled_for", name="uq_job_schedule"),
        Index("ix_persistent_job_claim", "status", "scheduled_for", "locked_until"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    job_name: Mapped[str] = mapped_column(String(80), nullable=False)
    scheduled_for: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
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
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


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
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class BrokerOrder(Base):
    """统一 OMS 订单；默认 adapter=simulated，不含任何真实下单副作用。"""

    __tablename__ = "broker_orders"
    __table_args__ = (
        UniqueConstraint("client_order_id", name="uq_broker_order_client_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    client_order_id: Mapped[str] = mapped_column(String(100), nullable=False)
    account: Mapped[str] = mapped_column(String(100), nullable=False)
    code: Mapped[str] = mapped_column(String(20), nullable=False)
    side: Mapped[str] = mapped_column(String(10), nullable=False)
    order_type: Mapped[str] = mapped_column(String(20), nullable=False)
    quantity: Mapped[float] = mapped_column(Numeric(20, 6), nullable=False)
    limit_price: Mapped[float | None] = mapped_column(Numeric(20, 6))
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    adapter: Mapped[str] = mapped_column(String(40), nullable=False, default="simulated")
    strategy_version_id: Mapped[int | None] = mapped_column(
        ForeignKey("strategy_versions.id")
    )
    risk_result: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class BrokerFill(Base):
    __tablename__ = "broker_fills"
    __table_args__ = (
        UniqueConstraint("adapter", "external_fill_id", name="uq_broker_fill_external"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(
        ForeignKey("broker_orders.id"), nullable=False, index=True
    )
    adapter: Mapped[str] = mapped_column(String(40), nullable=False)
    external_fill_id: Mapped[str] = mapped_column(String(100), nullable=False)
    quantity: Mapped[float] = mapped_column(Numeric(20, 6), nullable=False)
    price: Mapped[float] = mapped_column(Numeric(20, 6), nullable=False)
    fee: Mapped[float] = mapped_column(Numeric(20, 6), nullable=False)
    filled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class BrokerAccountLedger(Base):
    __tablename__ = "broker_account_ledgers"

    account: Mapped[str] = mapped_column(String(100), primary_key=True)
    adapter: Mapped[str] = mapped_column(String(40), nullable=False, default="simulated")
    cash: Mapped[float] = mapped_column(Numeric(20, 2), nullable=False)
    positions: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    reconciliation_status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="not_checked"
    )
    last_reconciled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


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
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
