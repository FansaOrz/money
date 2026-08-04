"""策略生命周期、模拟 OMS/RMS 与治理操作接口。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Header, HTTPException, status
from pydantic import Field
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.schemas.common import ConfiguredBaseModel
from app.services import oms, strategy_lifecycle

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
    db: Session = Depends(get_db),
) -> dict[str, object]:
    state = oms.set_kill_switch(db, account, enabled)
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
