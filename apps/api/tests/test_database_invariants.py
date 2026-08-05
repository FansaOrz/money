"""绕过应用层也不能写入负现金、非法订单或超额成交。"""

from datetime import UTC, datetime

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError


def test_database_rejects_negative_cash_and_invalid_order(db_session) -> None:
    with pytest.raises(IntegrityError):
        db_session.execute(
            text(
                """
                INSERT INTO broker_account_ledgers
                (account, adapter, cash, positions, position_lots,
                 reconciliation_status, updated_at, row_version)
                VALUES
                ('INVALID', 'simulated', -1, '{}', '{}', 'clean', :now, 1)
                """
            ),
            {"now": datetime.now(UTC)},
        )
        db_session.flush()
    db_session.rollback()
    with pytest.raises(IntegrityError):
        db_session.execute(
            text(
                """
                INSERT INTO broker_orders
                (client_order_id, account, code, side, order_type, quantity,
                 reference_price, status, adapter, risk_result, created_at, updated_at)
                VALUES
                ('invalid-order', 'A', '600001', 'buy', 'market', 0,
                 10, 'created', 'simulated', '{}', :now, :now)
                """
            ),
            {"now": datetime.now(UTC)},
        )
        db_session.flush()
    db_session.rollback()
