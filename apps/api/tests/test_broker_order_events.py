"""异步订单事件、重复成交、三方对账和人工接管。"""

from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from app.models import BrokerAccountLedger, BrokerOrder, ReconciliationRun
from app.services import broker_order_events, oms


def _order(db_session, key: str = "event-order") -> BrokerOrder:
    row = BrokerOrder(
        client_order_id=key,
        account="EVENT-A",
        code="600001",
        side="buy",
        order_type="market",
        quantity=100,
        reference_price=10,
        status="created",
        adapter="fake_async",
        risk_result={},
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    db_session.add(row)
    db_session.flush()
    return row


def test_out_of_order_duplicate_and_replay_are_deterministic(db_session) -> None:
    order = _order(db_session)
    broker_order_events.append_event(
        db_session,
        order_id=order.id,
        event_type="filled",
        adapter="fake_async",
        external_event_id="event-filled",
        broker_sequence=3,
    )
    broker_order_events.append_event(
        db_session,
        order_id=order.id,
        event_type="acknowledged",
        adapter="fake_async",
        external_event_id="event-ack",
        broker_sequence=2,
    )
    duplicate = broker_order_events.append_event(
        db_session,
        order_id=order.id,
        event_type="filled",
        adapter="fake_async",
        external_event_id="event-filled",
        broker_sequence=3,
    )
    assert duplicate.external_event_id == "event-filled"
    replay = broker_order_events.replay_order(db_session, order.id)
    assert replay["status"] == "filled"
    assert replay["event_count"] == 2


def test_duplicate_fill_does_not_mutate_ledger_twice(db_session) -> None:
    oms.initialize_simulated_account(db_session, "EVENT-A", 100_000)
    order = oms.submit_order(
        db_session,
        oms.OrderRequest(
            client_order_id="fill-idempotent",
            account="EVENT-A",
            code="600001",
            side="buy",
            quantity=100,
            reference_price=10,
        ),
        available_cash=0,
        available_position=0,
    )
    first = oms.simulate_fill(
        db_session,
        order.id,
        quantity=100,
        price=10,
        fee=5,
        external_fill_id="same-fill",
    )
    cash_after = float(
        db_session.get(BrokerAccountLedger, "EVENT-A").cash
    )
    second = oms.simulate_fill(
        db_session,
        order.id,
        quantity=100,
        price=10,
        fee=5,
        external_fill_id="same-fill",
    )
    assert second.id == first.id
    ledger = db_session.get(BrokerAccountLedger, "EVENT-A")
    assert float(ledger.cash) == cash_after
    reversal = oms.reverse_fill(
        db_session,
        original_fill_id=first.id,
        adjustment_external_fill_id="same-fill-reversed",
    )
    assert reversal.event_type == "reversal"
    assert float(ledger.cash) == 100_000


def test_rms_returns_independent_rejection_codes(db_session) -> None:
    request = oms.OrderRequest(
        client_order_id="rms-boundaries",
        account="RMS-A",
        code="600001",
        side="buy",
        quantity=100,
        reference_price=11,
    )
    result = oms.risk_check(
        db_session,
        request,
        available_cash=100_000,
        available_position=0,
        market_context={
            "security_allowed": False,
            "trading_session_open": False,
            "suspended": True,
            "at_price_limit": True,
            "quote": 10.0,
            "quote_age_seconds": 10,
            "broker_connected": False,
            "clock_offset_seconds": 2,
        },
    )
    codes = {item["code"] for item in result["rejections"]}
    assert {
        "SECURITY_PERMISSION",
        "TRADING_SESSION",
        "SUSPENDED",
        "PRICE_LIMIT",
        "PRICE_DEVIATION",
        "STALE_MARKET_DATA",
        "BROKER_DISCONNECTED",
        "CLOCK_SKEW",
    } <= codes


def test_reconciliation_persists_break_and_blocks_new_order(db_session) -> None:
    oms.initialize_simulated_account(db_session, "RECON-A", 100_000)
    result = oms.reconcile(
        db_session,
        "RECON-A",
        broker_cash=90_000,
        broker_positions={"600001": 100},
        broker_fills=[{"external_fill_id": "missing", "fee": 5}],
    )
    assert result["clean"] is False
    assert db_session.scalar(select(ReconciliationRun)) is not None
    with pytest.raises(ValueError, match="对账差异"):
        oms.submit_order(
            db_session,
            oms.OrderRequest(
                client_order_id="blocked-by-break",
                account="RECON-A",
                code="600001",
                side="buy",
                quantity=100,
                reference_price=10,
            ),
            available_cash=1_000_000,
            available_position=0,
        )


def test_manual_control_prevents_automatic_order(db_session) -> None:
    oms.initialize_simulated_account(db_session, "CONTROL-A", 100_000)
    oms.set_strategy_control_mode(
        db_session,
        account="CONTROL-A",
        strategy_version_id=None,
        mode="manual_control",
        reason="incident takeover",
        operator="operator-a",
        approver="approver-b",
    )
    with pytest.raises(ValueError, match="manual_control"):
        oms.submit_order(
            db_session,
            oms.OrderRequest(
                client_order_id="auto-after-takeover",
                account="CONTROL-A",
                code="600001",
                side="buy",
                quantity=100,
                reference_price=10,
            ),
            available_cash=100_000,
            available_position=0,
        )
