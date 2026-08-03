"""PDF 导入预览与正式落库服务。

预览数据只保留在进程内存中，不写入数据库；
确认导入时仅保存文件哈希、状态和数量摘要。
"""

from __future__ import annotations

import hashlib
import itertools
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from fastapi import HTTPException, UploadFile, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import (
    Account,
    Import,
    ImportStatus,
    Instrument,
    InstrumentType,
    Position,
    Transaction,
    TransactionType,
)
from app.schemas.imports import ImportPreviewResponse, PreviewPosition, PreviewTransaction
from app.schemas.portfolio import PortfolioSummary
from app.services.pdf_parser import ParsedDocument, PdfParseError, parse_pdf

_preview_ids = itertools.count(1)
_preview_sessions: dict[int, dict] = {}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _hash_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _prune_previews() -> None:
    ttl = timedelta(minutes=get_settings().import_session_ttl_minutes)
    expired = [key for key, item in _preview_sessions.items() if _now() - item["created_at"] > ttl]
    for key in expired:
        _preview_sessions.pop(key, None)


def _find_preview_by_hash(file_hash: str) -> tuple[int, dict] | None:
    _prune_previews()
    for import_id, item in _preview_sessions.items():
        if item["file_hash"] == file_hash:
            return import_id, item
    return None


def create_preview(db: Session, upload: UploadFile) -> ImportPreviewResponse:
    content = upload.file.read()
    if not content:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "上传文件为空")

    file_hash = hashlib.sha256(content).hexdigest()
    existing_import = db.scalar(select(Import).where(Import.file_hash == file_hash))
    if existing_import is not None and existing_import.status == ImportStatus.COMPLETED:
        raise HTTPException(status.HTTP_409_CONFLICT, "该文件已完成导入，不会重复写入")

    existing_session = _find_preview_by_hash(file_hash)
    if existing_session is not None:
        return _preview_response(existing_session[1])

    parsed: ParsedDocument
    try:
        parsed = parse_pdf(content)
    except PdfParseError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc

    if parsed.document_type == "transactions":
        duplicate_count = _count_existing_transactions(db, parsed)
        if duplicate_count == len(parsed.transactions):
            raise HTTPException(status.HTTP_409_CONFLICT, "该交易明细中的所有记录均已导入，不会重复写入")

    _prune_previews()
    import_id = next(_preview_ids)
    session = {
        "id": import_id,
        "filename": upload.filename or "未命名.pdf",
        "file_hash": file_hash,
        "created_at": _now(),
        "document_type": parsed.document_type,
        "snapshot_date": parsed.snapshot_date,
        "positions": parsed.positions,
        "transactions": parsed.transactions,
        "summary": parsed.summary,
        "warnings": parsed.warnings,
    }
    _preview_sessions[import_id] = session
    return _preview_response(session)


def _preview_response(session: dict) -> ImportPreviewResponse:
    positions = [PreviewPosition(**item) for item in session.get("positions", [])]
    transactions = [PreviewTransaction(**item) for item in session.get("transactions", [])]
    summary_data = session.get("summary")
    return ImportPreviewResponse(
        import_id=session["id"],
        file_name=session["filename"],
        document_type=session.get("document_type") or "unknown",
        snapshot_date=session.get("snapshot_date"),
        summary=PortfolioSummary(**summary_data) if summary_data else None,
        positions=positions,
        transactions=transactions,
        warnings=list(session.get("warnings", [])),
        status=ImportStatus.PENDING.value,
        message=None,
    )


def _count_existing_transactions(db: Session, parsed: ParsedDocument) -> int:
    count = 0
    for item in parsed.transactions:
        order_hash = _hash_text(item["order_no"])
        instrument = db.scalar(select(Instrument).where(Instrument.code == item["fund_code"]))
        if instrument is None:
            continue
        existing = db.scalar(
            select(Transaction.id).where(
                Transaction.external_order_hash == order_hash,
                Transaction.instrument_id == instrument.id,
                Transaction.source_type == item["source_type"],
            )
        )
        if existing is not None:
            count += 1
    return count


def commit_import(db: Session, import_id: int) -> tuple[int, int]:
    _prune_previews()
    session = _preview_sessions.get(import_id)
    if session is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "导入预览不存在或已过期，请重新上传")

    document_type = session["document_type"]
    if document_type == "positions":
        written = _commit_positions(db, session)
        message = f"已更新 {written} 条持仓"
    elif document_type == "transactions":
        written = _commit_transactions(db, session)
        message = f"已写入 {written} 条交易"
    else:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "未知导入类型")

    if document_type == "positions":
        _refresh_position_costs(db)

    record = Import(
        filename=session["filename"],
        file_hash=session["file_hash"],
        document_type=document_type,
        record_count=written,
        status=ImportStatus.COMPLETED,
        message=message,
        committed_at=_now(),
    )
    db.add(record)
    db.commit()
    _preview_sessions.pop(import_id, None)
    return (len(session.get("positions", [])), len(session.get("transactions", [])))


def _get_or_create_account(db: Session, account_key: str) -> Account:
    external_key = _hash_text(account_key)
    account = db.scalar(select(Account).where(Account.external_key == external_key))
    if account is None:
        account = Account(
            name="支付宝基金账户",
            institution="蚂蚁基金",
            external_key=external_key,
        )
        db.add(account)
        db.flush()
    return account


def _get_or_create_instrument(db: Session, code: str, name: str) -> Instrument:
    instrument = db.scalar(select(Instrument).where(Instrument.code == code))
    if instrument is None:
        instrument = Instrument(code=code, name=name, type=InstrumentType.FUND)
        db.add(instrument)
        db.flush()
    elif name and instrument.name != name:
        instrument.name = name
    return instrument


def _refresh_position_costs(db: Session) -> None:
    """按交易流水估算当前持仓成本。

    支付宝“持有收益”并不是严格的 FIFO。这里用交易记录计算每只基金剩余净投入：
    买入金额 - 卖出金额 - 分红金额。它能与支付宝的组合持有收益口径更接近，
    也不会把早期已清仓基金的历史亏损错误地摊到当前持仓上。
    """
    net_invested: dict[int, Decimal] = defaultdict(Decimal)
    for instrument_id, tx_type, amount in db.execute(
        select(Transaction.instrument_id, Transaction.type, Transaction.amount)
    ):
        if tx_type == TransactionType.BUY:
            net_invested[instrument_id] += amount
        elif tx_type in {TransactionType.SELL, TransactionType.DIVIDEND}:
            net_invested[instrument_id] -= amount

    positions = db.scalars(select(Position)).all()
    for position in positions:
        position.cost = max(net_invested.get(position.instrument_id, Decimal("0")), Decimal("0"))


def _commit_positions(db: Session, session: dict) -> int:
    written = 0
    positions_by_instrument: dict[int, Position] = {}
    for item in session.get("positions", []):
        account = _get_or_create_account(db, "alipay-fund-positions")
        instrument = _get_or_create_instrument(db, item["fund_code"], item["fund_name"])
        position = positions_by_instrument.get(instrument.id)
        if position is None:
            position = db.scalar(
                select(Position).where(
                    Position.account_id == account.id,
                    Position.instrument_id == instrument.id,
                )
            )
            if position is None:
                position = Position(
                    account_id=account.id,
                    instrument_id=instrument.id,
                    shares=Decimal("0"),
                    cost=Decimal("0"),
                    market_value=Decimal("0"),
                )
                db.add(position)
            else:
                # 资产证明是新的全量快照，同一基金多条记录重新汇总，不能在旧份额上累加。
                position.shares = Decimal("0")
                position.market_value = Decimal("0")
            positions_by_instrument[instrument.id] = position
        position.shares = position.shares + Decimal(item["shares"])
        position.latest_nav = Decimal(item["nav"])
        position.nav_date = datetime.strptime(item["nav_date"], "%Y-%m-%d").date()
        position.market_value = (position.market_value or Decimal("0")) + Decimal(item["market_value"])
        written += 1
    return written


def _commit_transactions(db: Session, session: dict) -> int:
    account = _get_or_create_account(db, "alipay-fund-transactions")
    written = 0
    for item in session.get("transactions", []):
        instrument = _get_or_create_instrument(db, item["fund_code"], item["fund_name"])
        order_hash = _hash_text(item["order_no"])
        existing = db.scalar(
            select(Transaction).where(
                Transaction.external_order_hash == order_hash,
                Transaction.instrument_id == instrument.id,
                Transaction.source_type == item["source_type"],
            )
        )
        if existing is not None:
            continue

        amount = Decimal(item["amount"])
        fee = Decimal(item["fee"])
        shares = Decimal(item["shares"]) if item["shares"] is not None else None
        nav = None
        if shares not in (None, Decimal("0")) and amount != 0:
            nav = (amount / abs(shares)).quantize(Decimal("0.0001"))
        transaction_type = TransactionType(item["transaction_type"])
        db.add(
            Transaction(
                external_order_hash=order_hash,
                source_type=item["source_type"],
                account_id=account.id,
                instrument_id=instrument.id,
                type=transaction_type,
                trade_date=datetime.strptime(item["transaction_date"], "%Y-%m-%d").date(),
                shares=shares,
                nav=nav,
                amount=amount,
                fee=fee,
            )
        )
        written += 1
    return written
