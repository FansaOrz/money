"""策略生命周期、模拟 OMS/RMS 与治理操作接口。"""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

from fastapi import APIRouter, Depends, Header, HTTPException, status
from pydantic import Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.config import get_settings
from app.models import (
    FactorHealthReport,
    FactorMonitorSnapshot,
    HoldoutConsumption,
    QuantDataRecord,
    StrategyTransition,
    StrategyVersion,
)
from app.schemas.common import ConfiguredBaseModel
from app.services import (
    corporate_action_master,
    data_quality_scan,
    execution_calibration,
    execution_reference_sync,
    factor_monitoring,
    oms,
    pit_warehouse,
    signal_lineage,
    strategy_lifecycle,
    platform_preflight,
)
from app.services.stock_repository import SqlStockRepository

router = APIRouter(prefix="/quant-governance", tags=["quant-governance"])


class TransitionRequest(ConfiguredBaseModel):
    to_status: str
    evidence: dict[str, object] = Field(default_factory=dict)
    reason: str = Field(min_length=1, max_length=1000)


class OrderIn(ConfiguredBaseModel):
    client_order_id: str
    account: str
    code: str
    side: str
    quantity: float
    reference_price: float
    order_type: str = "market"
    limit_price: float | None = None
    strategy_version_id: int | None = None
    available_cash: float = 0.0
    available_position: float = 0.0


class FillIn(ConfiguredBaseModel):
    quantity: float
    price: float
    fee: float = 0.0
    external_fill_id: str


class ReconcileIn(ConfiguredBaseModel):
    broker_cash: float
    broker_positions: dict[str, float] = Field(default_factory=dict)


class CorporateActionResolutionIn(ConfiguredBaseModel):
    resolution: dict[str, object]
    operator: str = Field(min_length=1, max_length=100)


class FactorPeriodMetricIn(ConfiguredBaseModel):
    as_of: date
    rank_ic: float | None = None
    top_minus_bottom_return: float | None = None
    turnover: float | None = None
    capacity_ratio: float | None = None
    maximum_peer_correlation: float | None = None
    exposure: float | None = None


class FactorMonitorIn(ConfiguredBaseModel):
    strategy_version_id: int | None = None
    history: list[FactorPeriodMetricIn] = Field(min_length=1)


@router.get("/preflight")
def high_risk_preflight(
    operation: str,
    target: str,
    impact: str,
    idempotency_key: str,
    db: Session = Depends(get_db),
) -> dict[str, object]:
    """返回当前服务端状态生成的摘要哈希；执行时必须原样二次确认。"""
    return platform_preflight.evaluate_system_preflight(
        db,
        operation=operation,
        target=target,
        impact=impact,
        idempotency_key=idempotency_key,
    )


@router.get("/data-sources/continuity")
def data_source_continuity(
    db: Session = Depends(get_db),
) -> dict[str, object]:
    return execution_reference_sync.provider_catalog(db)


@router.post("/data-quality/scan")
def run_data_quality_scan(
    db: Session = Depends(get_db),
) -> dict[str, object]:
    return data_quality_scan.scan_pit_warehouse(
        db,
        warehouse_path=Path(get_settings().research_db),
    )


@router.get("/signals/{signal_id}/{code}/lineage")
def signal_item_lineage(
    signal_id: int,
    code: str,
    db: Session = Depends(get_db),
) -> dict[str, object]:
    try:
        return signal_lineage.export_signal_lineage(
            db,
            signal_id=signal_id,
            code=code,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/factor-health")
def factor_health_reports(
    signal_date: date | None = None,
    status_filter: str | None = None,
    db: Session = Depends(get_db),
) -> list[dict[str, object]]:
    statement = select(FactorHealthReport).order_by(
        FactorHealthReport.signal_date.desc(),
        FactorHealthReport.factor_name,
    )
    if signal_date is not None:
        statement = statement.where(
            FactorHealthReport.signal_date == signal_date
        )
    if status_filter is not None:
        statement = statement.where(FactorHealthReport.status == status_filter)
    return [
        {
            "id": row.id,
            "strategy_version_id": row.strategy_version_id,
            "signal_date": row.signal_date.isoformat(),
            "factor": row.factor_name,
            "status": row.status,
            "unit": row.unit,
            "direction": row.direction,
            "statistics": row.statistics,
            "reasons": row.reasons,
        }
        for row in db.scalars(statement.limit(1000)).all()
    ]


@router.post("/factor-monitor/{factor_name}")
def run_factor_monitor(
    factor_name: str,
    payload: FactorMonitorIn,
    db: Session = Depends(get_db),
) -> dict[str, object]:
    decision = factor_monitoring.monitor_factor(
        factor_name,
        [
            factor_monitoring.FactorPeriodMetric(**item.model_dump())
            for item in payload.history
        ],
    )
    factor_monitoring.persist_monitor_decision(
        db,
        decision,
        strategy_version_id=payload.strategy_version_id,
    )
    db.commit()
    return {
        "factor": decision.factor,
        "as_of": decision.as_of.isoformat(),
        "action": decision.action,
        "weight_multiplier": decision.weight_multiplier,
        "reasons": list(decision.reasons),
        "metrics": decision.metrics,
    }


@router.get("/factor-monitor")
def list_factor_monitor(
    db: Session = Depends(get_db),
) -> list[dict[str, object]]:
    rows = db.scalars(
        select(FactorMonitorSnapshot)
        .order_by(FactorMonitorSnapshot.as_of.desc())
        .limit(1000)
    ).all()
    return [
        {
            "factor": row.factor_name,
            "as_of": row.as_of.isoformat(),
            "action": row.action,
            "metrics": row.metrics,
            "reasons": row.reasons,
            "policy_version": row.policy_version,
        }
        for row in rows
    ]


@router.get("/holdout-consumptions")
def list_holdout_consumptions(
    interval_start: date | None = None,
    interval_end: date | None = None,
    db: Session = Depends(get_db),
) -> list[dict[str, object]]:
    statement = select(HoldoutConsumption).order_by(
        HoldoutConsumption.consumed_at.desc(),
        HoldoutConsumption.id.desc(),
    )
    if interval_start is not None:
        statement = statement.where(
            HoldoutConsumption.interval_start == interval_start
        )
    if interval_end is not None:
        statement = statement.where(
            HoldoutConsumption.interval_end == interval_end
        )
    return [
        {
            "id": row.id,
            "experiment_id": row.experiment_id,
            "strategy_version_id": row.strategy_version_id,
            "interval": [
                row.interval_start.isoformat(),
                row.interval_end.isoformat(),
            ],
            "purpose": row.purpose,
            "status": row.status,
            "result_sha256": row.result_sha256,
            "consumed_by": row.consumed_by,
            "consumed_at": row.consumed_at.isoformat(),
        }
        for row in db.scalars(statement.limit(1000)).all()
    ]


@router.post("/pit-warehouse/build")
def build_pit_warehouse(
    db: Session = Depends(get_db),
) -> dict[str, object]:
    return pit_warehouse.build_pit_warehouse(
        db,
        research_root=Path(get_settings().research_data_dir),
    )


@router.get("/pit-warehouse/status")
def pit_warehouse_status() -> dict[str, object]:
    return pit_warehouse.pit_status(Path(get_settings().research_data_dir))


@router.get("/pit-warehouse/{dataset}/{code}/as-of")
def pit_warehouse_as_of(
    dataset: str,
    code: str,
    economic_as_of: date,
    system_as_of: datetime,
    limit: int = 1000,
) -> list[dict[str, object]]:
    try:
        return pit_warehouse.query_as_of(
            Path(get_settings().research_data_dir),
            dataset=dataset,
            code=code,
            economic_as_of=economic_as_of,
            system_as_of=system_as_of,
            limit=min(max(limit, 1), 10_000),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/corporate-actions/import-dividend-snapshot")
def import_corporate_action_snapshot(
    db: Session = Depends(get_db),
) -> dict[str, object]:
    root = (
        Path(get_settings().research_data_dir)
        / "tushare_snapshot"
        / "stocks"
        / "dividend"
    )
    if not root.is_dir():
        raise HTTPException(status_code=404, detail=f"分红快照目录不存在：{root}")
    return corporate_action_master.import_dividend_snapshot(db, root)


@router.get("/corporate-actions/{code}")
def corporate_action_timeline(
    code: str,
    start: date | None = None,
    end: date | None = None,
    system_as_of: datetime | None = None,
    db: Session = Depends(get_db),
) -> list[dict[str, object]]:
    return corporate_action_master.event_timeline(
        db,
        code=code,
        start=start,
        end=end,
        system_as_of=system_as_of,
    )


@router.get("/corporate-action-reviews")
def corporate_action_review_cases(
    case_status: str | None = "open",
    db: Session = Depends(get_db),
) -> list[dict[str, object]]:
    return corporate_action_master.list_review_cases(
        db,
        status=case_status,
    )


@router.post("/corporate-action-reviews/{case_id}/resolve")
def resolve_corporate_action_review(
    case_id: int,
    payload: CorporateActionResolutionIn,
    db: Session = Depends(get_db),
) -> dict[str, object]:
    try:
        return corporate_action_master.resolve_review_case(
            db,
            case_id=case_id,
            resolution=payload.resolution,
            operator=payload.operator,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/execution-calibration/run")
def run_execution_calibration(
    db: Session = Depends(get_db),
) -> dict[str, object]:
    return execution_calibration.calibrate_and_persist(db)


@router.get("/execution-calibration/latest")
def latest_execution_calibration(
    db: Session = Depends(get_db),
) -> dict[str, object]:
    row = db.scalar(
        select(QuantDataRecord)
        .where(QuantDataRecord.dataset == "execution_calibration")
        .order_by(QuantDataRecord.imported_at.desc())
        .limit(1)
    )
    if row is None:
        raise HTTPException(status_code=404, detail="尚无执行校准结果")
    return {
        **dict(row.payload or {}),
        "source_hash": row.source_hash,
        "generated_at": row.imported_at.isoformat(),
    }


@router.get("/index-weights/csi800/{as_of}")
def csi800_weights_as_of(
    as_of: date,
    db: Session = Depends(get_db),
) -> dict[str, object]:
    """查询指定历史日可用的 300+500 官方权重和完整源文件血缘。"""
    try:
        result = SqlStockRepository(db).combined_csi800_weights(as_of)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {
        "as_of": as_of.isoformat(),
        "method": result.method,
        "weight_sum": sum(weight for _code, weight in result.weights),
        "constituent_count": len(result.weights),
        "weights": dict(result.weights),
        "components": [
            {
                "index_code": item.index_code,
                "snapshot_date": item.snapshot_date.isoformat(),
                "constituent_count": len(item.weights),
                "weight_sum_percent": item.weight_sum_percent,
                "source_files": item.source_files,
                "source_hashes": item.source_hashes,
            }
            for item in result.component_snapshots
        ],
    }


@router.get("/strategies/{strategy_version_id}")
def strategy_governance_detail(
    strategy_version_id: int,
    db: Session = Depends(get_db),
) -> dict[str, object]:
    """查询冻结任务书、批准边界和逐次门禁实际值。"""

    version = db.get(StrategyVersion, strategy_version_id)
    if version is None:
        raise HTTPException(status_code=404, detail="策略版本不存在")
    transitions = db.scalars(
        select(StrategyTransition)
        .where(StrategyTransition.strategy_version_id == strategy_version_id)
        .order_by(StrategyTransition.id)
    ).all()
    mandate = dict(version.mandate or {})
    params = dict(version.params or {})
    return {
        "id": version.id,
        "name": version.name,
        "status": version.status,
        "mandate": mandate,
        "mandate_sha256": version.mandate_sha256,
        "validation_scope": mandate.get("validation_scope"),
        "investment_approval_eligible": bool(
            mandate.get("investment_approval_eligible", False)
        ),
        "approval_blocker": params.get("approval_blocker"),
        "transitions": [
            {
                "id": row.id,
                "from_status": row.from_status,
                "to_status": row.to_status,
                "approved": row.approved,
                "actor": row.actor,
                "reason": row.reason,
                "gates": row.gates,
                "created_at": row.created_at.isoformat(),
            }
            for row in transitions
        ],
    }


@router.post("/strategies/{strategy_version_id}/transition")
def transition_strategy(
    strategy_version_id: int,
    payload: TransitionRequest,
    actor: str = Header(default="local-admin", alias="X-Actor"),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    try:
        version = strategy_lifecycle.transition(
            db,
            strategy_version_id,
            payload.to_status,
            evidence=payload.evidence,
            actor=actor,
            reason=payload.reason,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc
    return {"id": version.id, "status": version.status}


@router.post("/orders")
def submit_simulated_order(
    payload: OrderIn,
    db: Session = Depends(get_db),
) -> dict[str, object]:
    try:
        order = oms.submit_order(
            db,
            oms.OrderRequest(
                client_order_id=payload.client_order_id,
                account=payload.account,
                code=payload.code,
                side=payload.side,
                quantity=payload.quantity,
                reference_price=payload.reference_price,
                order_type=payload.order_type,
                limit_price=payload.limit_price,
                strategy_version_id=payload.strategy_version_id,
            ),
            available_cash=payload.available_cash,
            available_position=payload.available_position,
        )
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc
    return {
        "id": order.id,
        "client_order_id": order.client_order_id,
        "status": order.status,
        "adapter": order.adapter,
        "risk_result": order.risk_result,
    }


@router.post("/orders/{order_id}/cancel")
def cancel_order(order_id: int, db: Session = Depends(get_db)) -> dict[str, object]:
    try:
        order = oms.cancel_order(db, order_id)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc
    return {"id": order.id, "status": order.status}


@router.post("/accounts/{account}/kill-switch")
def kill_switch(
    account: str,
    enabled: bool,
    x_actor: str = Header(default="api-operator", alias="X-Actor"),
    x_approver: str | None = Header(default=None, alias="X-Approver"),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    try:
        state = oms.set_kill_switch(
            db,
            account,
            enabled,
            actor=x_actor,
            approver=x_approver,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"account": state.account, "kill_switch": state.kill_switch}


@router.post("/accounts/{account}/initialize")
def initialize_account(
    account: str, cash: float, db: Session = Depends(get_db)
) -> dict[str, object]:
    try:
        ledger = oms.initialize_simulated_account(db, account, cash)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "account": ledger.account,
        "adapter": ledger.adapter,
        "cash": float(ledger.cash),
        "positions": ledger.positions,
    }


@router.post("/orders/{order_id}/fills")
def fill_order(
    order_id: int, payload: FillIn, db: Session = Depends(get_db)
) -> dict[str, object]:
    try:
        fill = oms.simulate_fill(
            db,
            order_id,
            quantity=payload.quantity,
            price=payload.price,
            fee=payload.fee,
            external_fill_id=payload.external_fill_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"id": fill.id, "order_id": fill.order_id}


@router.post("/accounts/{account}/reconcile")
def reconcile_account(
    account: str,
    payload: ReconcileIn,
    db: Session = Depends(get_db),
) -> dict[str, object]:
    try:
        return oms.reconcile(
            db,
            account,
            broker_cash=payload.broker_cash,
            broker_positions=payload.broker_positions,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
