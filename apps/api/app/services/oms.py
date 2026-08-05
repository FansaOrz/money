"""统一 OMS/RMS；当前只实现无真实副作用的 simulated adapter。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
import hashlib
import json
import time
from typing import Protocol

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import (
    BrokerFill,
    BrokerAccountLedger,
    BrokerOrder,
    KillSwitchDrill,
    ReconciliationBreak,
    ReconciliationRun,
    RiskControlState,
    StrategyControlState,
    StrategyVersion,
)
from app.services import trading_rules
from app.services import position_lots
from app.services import broker_order_events


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
    market_context: dict[str, object] | None = None,
) -> dict[str, object]:
    reasons: list[str] = []
    rejections: list[dict[str, object]] = []

    def reject(code: str, message: str, **detail: object) -> None:
        reasons.append(message)
        rejections.append({"code": code, "message": message, **detail})

    state = db.get(RiskControlState, request.account)
    if state is not None and state.kill_switch:
        reject("KILL_SWITCH", "账户紧急停止已开启")
    ledger = db.scalar(
        select(BrokerAccountLedger)
        .where(BrokerAccountLedger.account == request.account)
        .with_for_update()
    )
    if ledger is not None and ledger.reconciliation_status != "clean":
        reject("RECONCILIATION_BREAK", "账户存在未解决对账差异")
    control = db.scalar(
        select(StrategyControlState).where(
            StrategyControlState.account == request.account,
            StrategyControlState.strategy_version_id
            == request.strategy_version_id,
        )
    )
    if control is not None:
        if control.mode in {"paused", "manual_control", "recovering"}:
            reject("STRATEGY_CONTROL_MODE", f"策略处于 {control.mode} 状态")
        if control.mode == "reduce_only" and request.side == "buy":
            reject("REDUCE_ONLY", "策略处于只减仓状态")
    if request.side not in {"buy", "sell"}:
        reject("INVALID_SIDE", "side 必须为 buy/sell")
    if request.quantity <= 0 or request.reference_price <= 0:
        reject("INVALID_QUANTITY_OR_PRICE", "数量和参考价必须为正")
    quantity_rule = None
    if request.side in {"buy", "sell"} and request.quantity > 0:
        try:
            quantity_rule = trading_rules.quantity_rule(
                request.code, date.today()
            )
            for message in quantity_rule.validate(
                side=request.side,
                quantity=request.quantity,
                held=available_position,
                order_type=request.order_type,
            ):
                reject("QUANTITY_RULE", message)
        except ValueError as exc:
            reject("QUANTITY_RULE", str(exc))
    order_value = request.quantity * request.reference_price
    max_order_value = float(state.max_order_value) if state else 100_000.0
    if order_value > max_order_value:
        reject("MAX_ORDER_VALUE", f"订单金额超过单笔上限 {max_order_value:.2f}")
    if request.side == "buy" and order_value > available_cash:
        reject("INSUFFICIENT_CASH", "可用资金不足")
    if request.side == "sell" and request.quantity > available_position:
        reject("INSUFFICIENT_POSITION", "可用持仓不足")
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
        reject("MAX_DAILY_TURNOVER", f"当日累计成交将超过 {max_turnover:.2f}")
    if request.strategy_version_id is not None:
        version = db.get(StrategyVersion, request.strategy_version_id)
        if version is None or version.status not in {
            "paper_operational_validation",
            "paper",
            "approved",
            "live",
        }:
            reject("STRATEGY_STATUS", "策略版本状态不允许发单")
    duplicate = db.scalar(
        select(BrokerOrder.id).where(
            BrokerOrder.client_order_id == request.client_order_id
        )
    )
    if duplicate is not None:
        reject("DUPLICATE_CLIENT_ORDER_ID", "client_order_id 重复")
    context = market_context or {}
    if context:
        if context.get("security_allowed") is not True:
            reject("SECURITY_PERMISSION", "账户无该证券交易权限")
        if context.get("trading_session_open") is not True:
            reject("TRADING_SESSION", "当前不在允许交易时段")
        if context.get("suspended") is True:
            reject("SUSPENDED", "证券停牌")
        if context.get("at_price_limit") is True:
            reject("PRICE_LIMIT", "证券处于不可成交涨跌停")
        quote = context.get("quote")
        quote_age = context.get("quote_age_seconds")
        if not isinstance(quote, (int, float)) or float(quote) <= 0:
            reject("INVALID_QUOTE", "缺少有效权威行情")
        elif abs(request.reference_price / float(quote) - 1.0) > float(
            context.get("max_price_deviation", 0.03)
        ):
            reject("PRICE_DEVIATION", "订单价格偏离权威行情")
        if isinstance(quote_age, (int, float)) and float(quote_age) > 5.0:
            reject("STALE_MARKET_DATA", "行情超过5秒未更新")
        if context.get("broker_connected") is False:
            reject("BROKER_DISCONNECTED", "券商连接中断")
        if abs(float(context.get("clock_offset_seconds") or 0.0)) > 1.0:
            reject("CLOCK_SKEW", "系统时钟偏差超过1秒")
        for key, code, label in (
            ("gross_exposure_after", "GROSS_EXPOSURE", "总敞口"),
            ("industry_exposure_after", "INDUSTRY_EXPOSURE", "行业敞口"),
            ("style_exposure_after", "STYLE_EXPOSURE", "风格敞口"),
            ("beta_after", "BETA_LIMIT", "Beta"),
            ("tracking_error_after", "TRACKING_ERROR", "跟踪误差"),
            ("concentration_after", "CONCENTRATION", "集中度"),
        ):
            actual = context.get(key)
            limit = context.get(key.replace("_after", "_limit"))
            if isinstance(actual, (int, float)) and isinstance(limit, (int, float)):
                if abs(float(actual)) > float(limit):
                    reject(code, f"{label}超过账户/策略限制")
        open_orders = context.get("open_orders")
        if isinstance(open_orders, int) and open_orders >= int(
            context.get("max_open_orders", 100)
        ):
            reject("MAX_OPEN_ORDERS", "未完成订单数达到上限")
        if float(context.get("order_rate_per_minute") or 0.0) > float(
            context.get("max_order_rate_per_minute", 60)
        ):
            reject("ORDER_RATE", "下单频率超过限制")
        if float(context.get("cancel_ratio") or 0.0) > float(
            context.get("max_cancel_ratio", 0.80)
        ):
            reject("CANCEL_RATIO", "撤单率超过限制")
        if float(context.get("daily_loss") or 0.0) < -abs(
            float(context.get("max_daily_loss", 0.05))
        ):
            reject("DAILY_LOSS", "单日亏损超过限制")
        if float(context.get("drawdown") or 0.0) < -abs(
            float(context.get("max_drawdown", 0.10))
        ):
            reject("DRAWDOWN", "账户回撤超过限制")
    return {
        "passed": not reasons,
        "reasons": reasons,
        "rejections": rejections,
        "order_value": order_value,
        "quantity_rule_version": (
            quantity_rule.version if quantity_rule is not None else None
        ),
    }


def submit_order(
    db: Session,
    request: OrderRequest,
    *,
    available_cash: float,
    available_position: float,
) -> BrokerOrder:
    adapter = configured_adapter()
    ledger = db.scalar(
        select(BrokerAccountLedger)
        .where(BrokerAccountLedger.account == request.account)
        .with_for_update()
    )
    if ledger is not None:
        available_cash = float(ledger.cash)
        lot_ledger = position_lots.LotLedger.from_payload(
            dict(ledger.position_lots or {})
        )
        available_position = lot_ledger.available(request.code, date.today())
    elif adapter.name != "simulated":
        raise ValueError("真实适配器缺少权威账户账本，拒绝信任调用方余额")
    if (
        ledger is not None
        and adapter.name != "simulated"
        and (datetime.now(UTC) - ledger.updated_at).total_seconds() > 30
    ):
        raise ValueError("权威账户快照已陈旧，禁止新开仓")
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
        reference_price=Decimal(str(request.reference_price)),
        status="created",
        adapter=adapter.name,
        strategy_version_id=request.strategy_version_id,
        risk_result=result,
        created_at=now,
        updated_at=now,
    )
    db.add(order)
    db.flush()
    broker_id = adapter.submit(order)
    broker_order_events.append_event(
        db,
        order_id=order.id,
        event_type="submitted",
        adapter=adapter.name,
        external_event_id=f"{order.client_order_id}:submitted",
        broker_sequence=1,
        broker_order_id=broker_id,
    )
    broker_order_events.append_event(
        db,
        order_id=order.id,
        event_type="acknowledged",
        adapter=adapter.name,
        external_event_id=f"{order.client_order_id}:acknowledged",
        broker_sequence=2,
        broker_order_id=broker_id,
    )
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
            position_lots={},
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
    existing_fill = db.scalar(
        select(BrokerFill).where(
            BrokerFill.adapter == "simulated",
            BrokerFill.account == order.account,
            BrokerFill.trade_date == date.today(),
            BrokerFill.external_fill_id == external_fill_id,
        )
    )
    if existing_fill is not None:
        return existing_fill
    if order.status not in {"accepted", "accepted_simulated", "partially_filled"}:
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
    ledger = db.scalar(
        select(BrokerAccountLedger)
        .where(BrokerAccountLedger.account == order.account)
        .with_for_update()
    )
    if ledger is None:
        raise ValueError("模拟资金账户不存在")
    positions = dict(ledger.positions or {})
    lot_ledger = position_lots.LotLedger.from_payload(
        dict(ledger.position_lots or {})
    )
    amount = quantity * price
    now = datetime.now(UTC)
    consumed_lots: list[dict[str, object]] = []
    if order.side == "buy":
        if amount + fee > float(ledger.cash):
            raise ValueError("撮合时资金不足")
        ledger.cash = Decimal(str(float(ledger.cash) - amount - fee))
        lot_ledger.buy(
            order.code,
            quantity,
            amount + fee,
            acquired_date=now.date(),
            sellable_date=position_lots.next_calendar_settlement_day(now.date()),
            source=f"broker_fill:{external_fill_id}",
        )
        positions[order.code] = lot_ledger.total(order.code)
    else:
        if quantity > lot_ledger.available(order.code, now.date()) + 1e-9:
            raise ValueError("撮合时 T+1 可卖持仓不足")
        ledger.cash = Decimal(str(float(ledger.cash) + amount - fee))
        consumed_lots = lot_ledger.sell(
            order.code, quantity, trade_date=now.date()
        )
        remaining = lot_ledger.total(order.code)
        if remaining > 1e-9:
            positions[order.code] = remaining
        else:
            positions.pop(order.code, None)
    ledger.positions = positions
    ledger.position_lots = lot_ledger.to_payload()
    ledger.updated_at = now
    fill = BrokerFill(
        order_id=order.id,
        adapter="simulated",
        account=order.account,
        trade_date=now.date(),
        external_fill_id=external_fill_id,
        event_type="fill",
        quantity=Decimal(str(quantity)),
        price=Decimal(str(price)),
        fee=Decimal(str(fee)),
        fee_rule_version="BROKER_REPORTED",
        fee_breakdown={"broker_reported_total": fee},
        lot_consumption=consumed_lots,
        arrival_price=order.reference_price,
        decision_price=order.reference_price,
        implementation_shortfall=Decimal(
            str(
                (
                    price / float(order.reference_price) - 1.0
                    if order.side == "buy"
                    else 1.0 - price / float(order.reference_price)
                )
                if order.reference_price
                else 0.0
            )
        ),
        execution_session="broker_reported",
        slippage_model_version="BROKER_REPORTED",
        filled_at=now,
    )
    db.add(fill)
    final_event = (
        "filled"
        if filled_before + quantity >= float(order.quantity) - 1e-9
        else "partially_filled"
    )
    broker_order_events.append_event(
        db,
        order_id=order.id,
        event_type=final_event,
        adapter="simulated",
        external_event_id=f"fill-event:{order.account}:{now.date()}:{external_fill_id}",
        broker_fill_id=external_fill_id,
        payload={"quantity": quantity, "price": price, "fee": fee},
        occurred_at=now,
    )
    db.commit()
    db.refresh(fill)
    return fill


def reverse_fill(
    db: Session,
    *,
    original_fill_id: int,
    adjustment_external_fill_id: str,
) -> BrokerFill:
    """撤销券商成交并以独立负向记录审计，支持跨日补报。"""
    original = db.get(BrokerFill, original_fill_id)
    if original is None or original.event_type not in {"fill", "correction"}:
        raise ValueError("原成交不存在或已经是调整记录")
    existing = db.scalar(
        select(BrokerFill).where(
            BrokerFill.adapter == original.adapter,
            BrokerFill.account == original.account,
            BrokerFill.trade_date == date.today(),
            BrokerFill.external_fill_id == adjustment_external_fill_id,
        )
    )
    if existing is not None:
        return existing
    order = db.get(BrokerOrder, original.order_id)
    ledger = db.get(BrokerAccountLedger, original.account)
    if order is None or ledger is None:
        raise ValueError("原订单或账户账本不存在")
    quantity = float(original.quantity)
    amount = quantity * float(original.price)
    fee = float(original.fee)
    positions = {
        code: float(value) for code, value in dict(ledger.positions or {}).items()
    }
    lots = position_lots.LotLedger.from_payload(dict(ledger.position_lots or {}))
    now = datetime.now(UTC)
    if order.side == "buy":
        if positions.get(order.code, 0.0) + 1e-9 < quantity:
            raise ValueError("撤销买入成交会造成负持仓")
        ledger.cash = Decimal(str(float(ledger.cash) + amount + fee))
        remaining = positions.get(order.code, 0.0) - quantity
        old_lots = lots.remove(order.code)
        if remaining > 1e-9:
            total_before = sum(item.shares for item in old_lots)
            total_cost = sum(item.total_cost for item in old_lots)
            lots.buy(
                order.code,
                remaining,
                total_cost * remaining / max(total_before, quantity),
                acquired_date=now.date(),
                sellable_date=now.date(),
                source=f"fill_reversal_rebuild:{adjustment_external_fill_id}",
            )
            positions[order.code] = remaining
        else:
            positions.pop(order.code, None)
    else:
        ledger.cash = Decimal(str(float(ledger.cash) - amount + fee))
        if float(ledger.cash) < -1e-9:
            raise ValueError("撤销售出成交会造成负现金")
        lots.buy(
            order.code,
            quantity,
            amount,
            acquired_date=now.date(),
            sellable_date=now.date(),
            source=f"sell_fill_reversal:{adjustment_external_fill_id}",
        )
        positions[order.code] = lots.total(order.code)
    ledger.positions = positions
    ledger.position_lots = lots.to_payload()
    ledger.updated_at = now
    reversal = BrokerFill(
        order_id=order.id,
        adapter=original.adapter,
        account=original.account,
        trade_date=now.date(),
        external_fill_id=adjustment_external_fill_id,
        event_type="reversal",
        original_external_fill_id=original.external_fill_id,
        quantity=Decimal(str(-quantity)),
        price=original.price,
        fee=Decimal(str(-fee)),
        fee_rule_version=original.fee_rule_version,
        fee_breakdown={"reversal_of": original.external_fill_id},
        lot_consumption=[],
        filled_at=now,
    )
    db.add(reversal)
    broker_order_events.append_event(
        db,
        order_id=order.id,
        event_type="fill_cancelled",
        adapter=order.adapter,
        external_event_id=(
            f"fill-reversal-event:{original.account}:"
            f"{adjustment_external_fill_id}"
        ),
        broker_fill_id=adjustment_external_fill_id,
        payload={"original_external_fill_id": original.external_fill_id},
        occurred_at=now,
    )
    db.commit()
    db.refresh(reversal)
    return reversal


def correct_fill(
    db: Session,
    *,
    original_fill_id: int,
    reversal_external_fill_id: str,
    corrected_external_fill_id: str,
    quantity: float,
    price: float,
    fee: float,
) -> BrokerFill:
    """先撤销原成交再应用更正成交，两个步骤均幂等且可重放。"""
    original = db.get(BrokerFill, original_fill_id)
    if original is None:
        raise ValueError("原成交不存在")
    reverse_fill(
        db,
        original_fill_id=original_fill_id,
        adjustment_external_fill_id=reversal_external_fill_id,
    )
    corrected = simulate_fill(
        db,
        original.order_id,
        quantity=quantity,
        price=price,
        fee=fee,
        external_fill_id=corrected_external_fill_id,
    )
    corrected.event_type = "correction"
    corrected.original_external_fill_id = original.external_fill_id
    db.commit()
    return corrected


def resolve_reconciliation_break(
    db: Session,
    *,
    break_id: int,
    actor: str,
    resolution: dict[str, object],
) -> ReconciliationBreak:
    row = db.get(ReconciliationBreak, break_id)
    if row is None:
        raise ValueError("对账差异不存在")
    if row.status == "resolved":
        return row
    row.status = "resolved"
    row.owner = actor
    row.resolution = {**resolution, "actor": actor}
    row.resolved_at = datetime.now(UTC)
    remaining = db.scalar(
        select(func.count(ReconciliationBreak.id)).where(
            ReconciliationBreak.run_id == row.run_id,
            ReconciliationBreak.status == "open",
            ReconciliationBreak.id != row.id,
        )
    )
    run = db.get(ReconciliationRun, row.run_id)
    if run is not None and int(remaining or 0) == 0:
        run.status = "resolved"
        ledger = db.get(BrokerAccountLedger, run.account)
        if ledger is not None:
            ledger.reconciliation_status = "clean"
    db.commit()
    db.refresh(row)
    return row


def reconcile(
    db: Session,
    account: str,
    *,
    broker_cash: float,
    broker_positions: dict[str, float],
    tolerance: float = 0.01,
    broker_orders: list[dict[str, object]] | None = None,
    broker_fills: list[dict[str, object]] | None = None,
    broker_cash_flows: list[dict[str, object]] | None = None,
    owner: str = "operations",
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
    local_orders = list(
        db.scalars(select(BrokerOrder).where(BrokerOrder.account == account)).all()
    )
    local_fills = list(
        db.scalars(select(BrokerFill).where(BrokerFill.account == account)).all()
    )
    broker_snapshot = {
        "cash": broker_cash,
        "positions": broker_positions,
        "orders": broker_orders or [],
        "fills": broker_fills or [],
        "cash_flows": broker_cash_flows or [],
    }
    local_snapshot = {
        "cash": float(ledger.cash),
        "positions": local_positions,
        "orders": [
            {"client_order_id": row.client_order_id, "status": row.status}
            for row in local_orders
        ],
        "fills": [
            {
                "external_fill_id": row.external_fill_id,
                "quantity": float(row.quantity),
                "fee": float(row.fee),
            }
            for row in local_fills
        ],
    }
    broker_fill_ids = {
        str(row.get("external_fill_id")) for row in (broker_fills or [])
    }
    local_fill_ids = {row.external_fill_id for row in local_fills}
    missing_local_fills = sorted(broker_fill_ids - local_fill_ids)
    missing_broker_fills = sorted(local_fill_ids - broker_fill_ids) if broker_fills is not None else []
    fee_difference = (
        sum(float(row.get("fee") or 0.0) for row in (broker_fills or []))
        - sum(float(row.fee) for row in local_fills)
        if broker_fills is not None
        else 0.0
    )
    categories = {
        "cash": abs(cash_difference) > tolerance,
        "position": bool(position_differences),
        "missing_local_order_or_fill": missing_local_fills,
        "missing_broker_fill": missing_broker_fills,
        "fee": abs(fee_difference) > tolerance,
        "corporate_action_or_in_transit": [
            row
            for row in (broker_cash_flows or [])
            if row.get("kind") in {"corporate_action", "in_transit"}
        ],
    }
    clean = clean and not missing_local_fills and not missing_broker_fills and abs(fee_difference) <= tolerance
    now = datetime.now(UTC)
    snapshot_hash = hashlib.sha256(
        json.dumps(broker_snapshot, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    run = ReconciliationRun(
        account=account,
        adapter=ledger.adapter,
        trade_date=now.date(),
        status="clean" if clean else "break",
        broker_snapshot_sha256=snapshot_hash,
        broker_snapshot=broker_snapshot,
        local_snapshot=local_snapshot,
        tolerance={"cash": tolerance, "position": tolerance, "fee": tolerance},
        categories=categories,
        responsible_owner=owner,
        started_at=now,
        completed_at=now,
    )
    db.add(run)
    db.flush()
    if abs(cash_difference) > tolerance:
        db.add(ReconciliationBreak(
            run_id=run.id, break_type="cash", code=None,
            expected={"local": float(ledger.cash)}, actual={"broker": broker_cash},
            difference={"amount": cash_difference}, status="open", owner=owner,
            resolution={}, created_at=now,
        ))
    for code, difference in position_differences.items():
        db.add(ReconciliationBreak(
            run_id=run.id, break_type="position", code=code,
            expected={"local": local_positions.get(code, 0.0)},
            actual={"broker": broker_positions.get(code, 0.0)},
            difference={"quantity": difference}, status="open", owner=owner,
            resolution={}, created_at=now,
        ))
    for fill_id in missing_local_fills:
        db.add(ReconciliationBreak(
            run_id=run.id, break_type="missing_fill", code=None,
            expected={"local": None}, actual={"broker_fill_id": fill_id},
            difference={"missing": "local"}, status="open", owner=owner,
            resolution={}, created_at=now,
        ))
    if abs(fee_difference) > tolerance:
        db.add(ReconciliationBreak(
            run_id=run.id, break_type="fee", code=None,
            expected={"local_total": sum(float(row.fee) for row in local_fills)},
            actual={"broker_total": sum(float(row.get("fee") or 0.0) for row in (broker_fills or []))},
            difference={"amount": fee_difference}, status="open", owner=owner,
            resolution={}, created_at=now,
        ))
    ledger.reconciliation_status = "clean" if clean else "break"
    ledger.last_reconciled_at = datetime.now(UTC)
    db.commit()
    return {
        "clean": clean,
        "cash_difference": cash_difference,
        "position_differences": position_differences,
        "run_id": run.id,
        "categories": categories,
        "broker_snapshot_sha256": snapshot_hash,
    }


def cancel_order(db: Session, order_id: int) -> BrokerOrder:
    order = db.get(BrokerOrder, order_id)
    if order is None:
        raise ValueError("订单不存在")
    if order.status not in {"accepted", "accepted_simulated", "partially_filled"}:
        raise ValueError(f"订单状态 {order.status} 不可撤")
    configured_adapter().cancel(order)
    broker_order_events.append_event(
        db,
        order_id=order.id,
        event_type="cancelled",
        adapter=order.adapter,
        external_event_id=f"{order.client_order_id}:cancelled",
    )
    db.commit()
    return order


def set_kill_switch(
    db: Session,
    account: str,
    enabled: bool,
    *,
    actor: str = "system",
    approver: str | None = None,
    sla_ms: int = 5_000,
) -> RiskControlState:
    if enabled and (not approver or approver == actor):
        raise ValueError("开启 kill switch 需要不同主体二次确认")
    started = time.monotonic()
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
    cancelled: list[int] = []
    failed: list[int] = []
    if enabled:
        open_orders = list(
            db.scalars(
                select(BrokerOrder).where(
                    BrokerOrder.account == account,
                    BrokerOrder.status.in_(
                        {"submitted", "accepted", "partially_filled", "cancel_pending"}
                    ),
                )
            ).all()
        )
        for order in open_orders:
            try:
                configured_adapter().cancel(order)
                broker_order_events.append_event(
                    db,
                    order_id=order.id,
                    event_type="cancelled",
                    adapter=order.adapter,
                    external_event_id=f"kill-switch:{order.id}:{datetime.now(UTC).timestamp()}",
                    payload={"actor": actor, "approver": approver},
                )
                cancelled.append(order.id)
            except Exception:
                failed.append(order.id)
        elapsed_ms = int((time.monotonic() - started) * 1000)
        db.add(KillSwitchDrill(
            account=account, triggered_by=actor, approved_by=str(approver),
            policy={"new_orders": "reject", "open_orders": "cancel", "positions": "hold_or_reduce_only"},
            cancelled_orders=cancelled, failed_orders=failed,
            elapsed_ms=elapsed_ms, sla_ms=sla_ms,
            passed=not failed and elapsed_ms <= sla_ms,
            created_at=datetime.now(UTC),
        ))
    db.commit()
    db.refresh(state)
    return state


def set_strategy_control_mode(
    db: Session,
    *,
    account: str,
    strategy_version_id: int | None,
    mode: str,
    reason: str,
    operator: str,
    approver: str | None,
    scope: dict[str, object] | None = None,
) -> StrategyControlState:
    if mode not in {
        "running", "paused", "reduce_only", "manual_control", "recovering"
    }:
        raise ValueError("策略接管状态非法")
    if mode in {"manual_control", "running"} and (
        not approver or approver == operator
    ):
        raise ValueError("人工接管/恢复运行需要不同主体批准")
    row = db.scalar(
        select(StrategyControlState).where(
            StrategyControlState.account == account,
            StrategyControlState.strategy_version_id == strategy_version_id,
        )
    )
    if row is None:
        row = StrategyControlState(
            account=account,
            strategy_version_id=strategy_version_id,
            mode=mode,
            reason=reason,
            operator=operator,
            approver=approver,
            scope=scope or {},
            updated_at=datetime.now(UTC),
        )
    else:
        row.mode = mode
        row.reason = reason
        row.operator = operator
        row.approver = approver
        row.scope = scope or {}
        row.updated_at = datetime.now(UTC)
    db.add(row)
    db.commit()
    db.refresh(row)
    return row
