"""统一 OMS/RMS；当前只实现无真实副作用的 simulated adapter。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Protocol

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import (
    BrokerFill,
    BrokerAccountLedger,
    BrokerOrder,
    RiskControlState,
    StrategyVersion,
)


@dataclass(frozen=True)
class OrderRequest:
    client_order_id: str
    account: str
    code: str
    side: str
    quantity: float
    reference_price: float
    order_type: str = "market"
    limit_price: float | None = None
    strategy_version_id: int | None = None


class BrokerAdapter(Protocol):
    name: str

    def submit(self, order: BrokerOrder) -> str: ...
    def cancel(self, order: BrokerOrder) -> None: ...


class SimulatedAdapter:
    name = "simulated"

    def submit(self, order: BrokerOrder) -> str:
        return f"SIM-{order.client_order_id}"

    def cancel(self, order: BrokerOrder) -> None:
        return None


def configured_adapter() -> BrokerAdapter:
    settings = get_settings()
    if settings.broker_adapter != "simulated":
        raise RuntimeError(
            "未安装真实券商适配器；为避免真实下单副作用，系统保持 simulated"
        )
    return SimulatedAdapter()


def risk_check(
    db: Session,
    request: OrderRequest,
    *,
    available_cash: float,
    available_position: float,
) -> dict[str, object]:
    reasons: list[str] = []
    state = db.get(RiskControlState, request.account)
    if state is not None and state.kill_switch:
        reasons.append("账户紧急停止已开启")
    if request.side not in {"buy", "sell"}:
        reasons.append("side 必须为 buy/sell")
    if request.quantity <= 0 or request.reference_price <= 0:
        reasons.append("数量和参考价必须为正")
    if request.side == "buy" and request.quantity % 100 != 0:
        reasons.append("A股买入数量必须为100股整数倍")
    order_value = request.quantity * request.reference_price
    max_order_value = float(state.max_order_value) if state else 100_000.0
    if order_value > max_order_value:
        reasons.append(f"订单金额超过单笔上限 {max_order_value:.2f}")
    if request.side == "buy" and order_value > available_cash:
        reasons.append("可用资金不足")
    if request.side == "sell" and request.quantity > available_position:
        reasons.append("可用持仓不足")
    today_turnover = db.scalar(
        select(func.coalesce(func.sum(BrokerFill.quantity * BrokerFill.price), 0))
        .join(BrokerOrder, BrokerOrder.id == BrokerFill.order_id)
        .where(
            BrokerOrder.account == request.account,
            func.date(BrokerFill.filled_at) == date.today().isoformat(),
        )
    )
    max_turnover = float(state.max_daily_turnover) if state else 500_000.0
    if float(today_turnover or 0) + order_value > max_turnover:
        reasons.append(f"当日累计成交将超过 {max_turnover:.2f}")
    if request.strategy_version_id is not None:
        version = db.get(StrategyVersion, request.strategy_version_id)
        if version is None or version.status not in {"paper", "approved", "live"}:
            reasons.append("策略版本状态不允许发单")
    duplicate = db.scalar(
        select(BrokerOrder.id).where(
            BrokerOrder.client_order_id == request.client_order_id
        )
    )
    if duplicate is not None:
        reasons.append("client_order_id 重复")
    return {"passed": not reasons, "reasons": reasons, "order_value": order_value}


def submit_order(
    db: Session,
    request: OrderRequest,
    *,
    available_cash: float,
    available_position: float,
) -> BrokerOrder:
    adapter = configured_adapter()
    ledger = db.get(BrokerAccountLedger, request.account)
    if ledger is not None:
        available_cash = float(ledger.cash)
        available_position = float((ledger.positions or {}).get(request.code, 0.0))
    result = risk_check(
        db,
        request,
        available_cash=available_cash,
        available_position=available_position,
    )
    if not result["passed"]:
        raise ValueError("；".join(result["reasons"]))  # type: ignore[arg-type]
    now = datetime.now(UTC)
    order = BrokerOrder(
        client_order_id=request.client_order_id,
        account=request.account,
        code=request.code,
        side=request.side,
        order_type=request.order_type,
        quantity=Decimal(str(request.quantity)),
        limit_price=(
            Decimal(str(request.limit_price))
            if request.limit_price is not None
            else None
        ),
        status="accepted_simulated",
        adapter=adapter.name,
        strategy_version_id=request.strategy_version_id,
        risk_result=result,
        created_at=now,
        updated_at=now,
    )
    db.add(order)
    db.flush()
    adapter.submit(order)
    db.commit()
    db.refresh(order)
    return order


def initialize_simulated_account(
    db: Session, account: str, cash: float
) -> BrokerAccountLedger:
    if cash < 0:
        raise ValueError("初始现金不能为负")
    ledger = db.get(BrokerAccountLedger, account)
    now = datetime.now(UTC)
    if ledger is None:
        ledger = BrokerAccountLedger(
            account=account,
            adapter="simulated",
            cash=Decimal(str(cash)),
            positions={},
            reconciliation_status="clean",
            last_reconciled_at=now,
            updated_at=now,
        )
        db.add(ledger)
        db.commit()
        db.refresh(ledger)
        return ledger
    if ledger.positions or float(ledger.cash) != cash:
        raise ValueError("账户已存在，拒绝覆盖资金或持仓")
    return ledger


def simulate_fill(
    db: Session,
    order_id: int,
    *,
    quantity: float,
    price: float,
    fee: float = 0.0,
    external_fill_id: str,
) -> BrokerFill:
    order = db.get(BrokerOrder, order_id)
    if order is None or order.adapter != "simulated":
        raise ValueError("仅允许撮合 simulated 订单")
    if order.status not in {"accepted_simulated", "partially_filled"}:
        raise ValueError(f"订单状态 {order.status} 不可成交")
    filled_before = float(
        db.scalar(
            select(func.coalesce(func.sum(BrokerFill.quantity), 0)).where(
                BrokerFill.order_id == order.id
            )
        )
        or 0
    )
    if quantity <= 0 or filled_before + quantity > float(order.quantity) + 1e-9:
        raise ValueError("成交数量无效或超过订单数量")
    ledger = db.get(BrokerAccountLedger, order.account)
    if ledger is None:
        raise ValueError("模拟资金账户不存在")
    positions = dict(ledger.positions or {})
    held = float(positions.get(order.code, 0.0))
    amount = quantity * price
    if order.side == "buy":
        if amount + fee > float(ledger.cash):
            raise ValueError("撮合时资金不足")
        ledger.cash = Decimal(str(float(ledger.cash) - amount - fee))
        positions[order.code] = held + quantity
    else:
        if quantity > held:
            raise ValueError("撮合时持仓不足")
        ledger.cash = Decimal(str(float(ledger.cash) + amount - fee))
        remaining = held - quantity
        if remaining > 1e-9:
            positions[order.code] = remaining
        else:
            positions.pop(order.code, None)
    now = datetime.now(UTC)
    ledger.positions = positions
    ledger.updated_at = now
    fill = BrokerFill(
        order_id=order.id,
        adapter="simulated",
        external_fill_id=external_fill_id,
        quantity=Decimal(str(quantity)),
        price=Decimal(str(price)),
        fee=Decimal(str(fee)),
        filled_at=now,
    )
    db.add(fill)
    order.status = (
        "filled"
        if filled_before + quantity >= float(order.quantity) - 1e-9
        else "partially_filled"
    )
    order.updated_at = now
    db.commit()
    db.refresh(fill)
    return fill


def reconcile(
    db: Session,
    account: str,
    *,
    broker_cash: float,
    broker_positions: dict[str, float],
    tolerance: float = 0.01,
) -> dict[str, object]:
    ledger = db.get(BrokerAccountLedger, account)
    if ledger is None:
        raise ValueError("账户不存在")
    cash_difference = broker_cash - float(ledger.cash)
    local_positions = {
        code: float(quantity) for code, quantity in (ledger.positions or {}).items()
    }
    position_differences = {
        code: broker_positions.get(code, 0.0) - local_positions.get(code, 0.0)
        for code in set(broker_positions) | set(local_positions)
        if abs(broker_positions.get(code, 0.0) - local_positions.get(code, 0.0))
        > tolerance
    }
    clean = abs(cash_difference) <= tolerance and not position_differences
    ledger.reconciliation_status = "clean" if clean else "break"
    ledger.last_reconciled_at = datetime.now(UTC)
    db.commit()
    return {
        "clean": clean,
        "cash_difference": cash_difference,
        "position_differences": position_differences,
    }


def cancel_order(db: Session, order_id: int) -> BrokerOrder:
    order = db.get(BrokerOrder, order_id)
    if order is None:
        raise ValueError("订单不存在")
    if order.status not in {"accepted_simulated", "partially_filled"}:
        raise ValueError(f"订单状态 {order.status} 不可撤")
    configured_adapter().cancel(order)
    order.status = "cancelled"
    order.updated_at = datetime.now(UTC)
    db.commit()
    return order


def set_kill_switch(db: Session, account: str, enabled: bool) -> RiskControlState:
    state = db.get(RiskControlState, account)
    if state is None:
        state = RiskControlState(
            account=account,
            kill_switch=enabled,
            max_order_value=Decimal("100000"),
            max_daily_turnover=Decimal("500000"),
            updated_at=datetime.now(UTC),
        )
    else:
        state.kill_switch = enabled
        state.updated_at = datetime.now(UTC)
    db.add(state)
    db.commit()
    db.refresh(state)
    return state
