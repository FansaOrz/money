"""交易流水查询接口。"""

from sqlalchemy import func, select
from sqlalchemy.orm import Session, joinedload
from fastapi import APIRouter, Depends, Query

from app.db.session import get_db
from app.models import Transaction
from app.schemas.imports import PreviewTransaction

router = APIRouter(prefix="/transactions", tags=["transactions"])


@router.get("", response_model=list[PreviewTransaction])
def list_transactions(
    fund_code: str | None = Query(default=None),
    transaction_type: str | None = Query(default=None),
    limit: int = Query(default=1000, ge=1, le=5000),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
) -> list[PreviewTransaction]:
    stmt = (
        select(Transaction)
        .options(joinedload(Transaction.instrument))
        .order_by(Transaction.trade_date.desc(), Transaction.id.desc())
        .limit(limit)
        .offset(offset)
    )
    if fund_code:
        stmt = stmt.join(Transaction.instrument).where(Transaction.instrument.has(code=fund_code))
    if transaction_type:
        stmt = stmt.where(Transaction.type == transaction_type)

    return [
        PreviewTransaction(
            transaction_date=transaction.trade_date.isoformat(),
            confirmation_date=transaction.trade_date.isoformat(),
            fund_code=transaction.instrument.code,
            fund_name=transaction.instrument.name,
            transaction_type=transaction.type.value,
            amount=transaction.amount,
            shares=transaction.shares,
            nav=transaction.nav,
            fee=transaction.fee,
            status="已导入",
        )
        for transaction in db.scalars(stmt).all()
    ]
