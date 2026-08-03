"""指数成分（universe）管理。

- sync_index_cons：ak.index_stock_cons_csindex -> index_constituents（当前成分，全量替换）。
- import_membership_events_csv：导入指数成分调整事件（add/remove），幂等。
- replay_snapshot：按事件流重建某历史日期的成分，物化到 stock_universe_snapshots。
- get_universe：查询某指数某日期成分（当前直接读 index_constituents；
  历史日期优先读物化快照，其次实时回放事件）。

注意：历史成分的完整性取决于事件 CSV 的覆盖范围；缺事件时快照只反映
“已知事件回放结果”，本模块不补齐、不猜测。
"""

from __future__ import annotations

import csv
import io
import logging
from datetime import UTC, date, datetime
from typing import Any

import pandas as pd
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.models import IndexConstituent, IndexMembershipEvent, StockUniverseSnapshot
from app.services.research import ak_fetch
from app.services.research.stock_data import (
    _begin_task,
    _final_status,
    _finish_task,
    _to_date,
)

logger = logging.getLogger(__name__)

# 默认跟踪的指数：沪深300 / 中证500
TRACKED_INDEXES: dict[str, str] = {
    "000300": "沪深300",
    "000905": "中证500",
}

EVENT_COLUMNS = {"index_code", "stock_code", "event_type", "effective_date"}


# ---------------------------------------------------------------------------
# 当前成分
# ---------------------------------------------------------------------------

def sync_index_cons(
    db: Session, index_codes: list[str] | None = None
) -> dict[str, Any]:
    """同步指数当前成分（全量替换对应指数行）。单指数失败不影响其他。"""
    state = _begin_task(db, "universe")
    codes = index_codes or list(TRACKED_INDEXES)
    updated = 0
    failed = 0
    errors: list[str] = []
    total_rows = 0
    for index_code in codes:
        frame = ak_fetch.fetch_index_cons(index_code)
        if frame is None:
            failed += 1
            errors.append(f"{index_code}: 数据源不可用")
            continue
        rows = _parse_cons_frame(index_code, frame)
        db.execute(delete(IndexConstituent).where(IndexConstituent.index_code == index_code))
        for row in rows:
            db.add(IndexConstituent(**row))
        db.commit()
        total_rows += len(rows)
        updated += 1
    status = _final_status(updated, failed, processed=len(codes))
    _finish_task(
        db, state, total=len(codes), updated=updated, failed=failed,
        detail="; ".join(errors[:20]) or None, status=status,
    )
    return {
        "task": "universe",
        "status": status,
        "total": len(codes),
        "updated": updated,
        "failed": failed,
        "rows": total_rows,
        "errors": errors,
    }


def _parse_cons_frame(index_code: str, frame: pd.DataFrame) -> list[dict[str, Any]]:
    """归一化中证指数成分表（列名兼容 csindex 中英文）。"""
    rows: list[dict[str, Any]] = []
    index_name = TRACKED_INDEXES.get(index_code)
    for _, record in frame.iterrows():
        stock_code = str(
            record.get("成分券代码") or record.get("stock_code") or ""
        ).strip().zfill(6)
        if len(stock_code) != 6 or not stock_code.isdigit():
            continue
        stock_name = record.get("成分券名称") or record.get("stock_name")
        in_date = _to_date(record.get("纳入日期") or record.get("date"))
        rows.append(
            {
                "index_code": index_code,
                "index_name": index_name,
                "stock_code": stock_code,
                "stock_name": str(stock_name).strip() if stock_name else None,
                "in_date": in_date,
            }
        )
    # 去重（同一股票可能出现多次时保留首行）
    seen: set[str] = set()
    unique_rows = []
    for row in rows:
        if row["stock_code"] in seen:
            continue
        seen.add(row["stock_code"])
        unique_rows.append(row)
    return unique_rows


# ---------------------------------------------------------------------------
# 事件 CSV 导入
# ---------------------------------------------------------------------------

def import_membership_events_csv(
    db: Session, content: bytes | str, *, source: str = "csv"
) -> dict[str, Any]:
    """导入成分调整事件 CSV。

    必需列：index_code, stock_code, event_type(add/remove), effective_date(YYYY-MM-DD)；
    可选列：stock_name, available_at(ISO 时间)。重复事件幂等跳过。
    """
    text = content.decode("utf-8-sig") if isinstance(content, bytes) else content
    reader = csv.DictReader(io.StringIO(text))
    if reader.fieldnames is None or not EVENT_COLUMNS.issubset(set(reader.fieldnames)):
        return {
            "status": "failed",
            "imported": 0,
            "skipped": 0,
            "errors": [f"CSV 缺少必需列：{sorted(EVENT_COLUMNS)}"],
        }

    imported = 0
    skipped = 0
    errors: list[str] = []
    for lineno, row in enumerate(reader, start=2):
        index_code = (row.get("index_code") or "").strip()
        stock_code = (row.get("stock_code") or "").strip().zfill(6)
        event_type = (row.get("event_type") or "").strip().lower()
        effective_date = _to_date(row.get("effective_date"))
        if not index_code or event_type not in {"add", "remove"} or effective_date is None:
            errors.append(f"第 {lineno} 行字段非法，已跳过")
            continue
        available_at = None
        raw_available = (row.get("available_at") or "").strip()
        if raw_available:
            try:
                available_at = datetime.fromisoformat(raw_available)
            except ValueError:
                errors.append(f"第 {lineno} 行 available_at 无法解析，置空")
        exists = db.scalar(
            select(IndexMembershipEvent.id).where(
                IndexMembershipEvent.index_code == index_code,
                IndexMembershipEvent.stock_code == stock_code,
                IndexMembershipEvent.effective_date == effective_date,
                IndexMembershipEvent.event_type == event_type,
            )
        )
        if exists is not None:
            skipped += 1
            continue
        db.add(
            IndexMembershipEvent(
                index_code=index_code,
                stock_code=stock_code,
                stock_name=(row.get("stock_name") or "").strip() or None,
                event_type=event_type,
                effective_date=effective_date,
                source=source,
                available_at=available_at,
            )
        )
        imported += 1
    db.commit()
    return {
        "status": "success" if not errors else "success",
        "imported": imported,
        "skipped": skipped,
        "errors": errors,
    }


# ---------------------------------------------------------------------------
# 快照
# ---------------------------------------------------------------------------

def replay_membership(db: Session, index_code: str, as_of: date) -> dict[str, str | None]:
    """按事件流回放 as_of 当日收盘后的成分（stock_code -> stock_name）。

    规则：按 effective_date 升序应用事件，add 加入、remove 剔除；
    同日同股先 remove 后 add 以 add 为准（dict 覆盖语义）。
    """
    events = db.scalars(
        select(IndexMembershipEvent)
        .where(
            IndexMembershipEvent.index_code == index_code,
            IndexMembershipEvent.effective_date <= as_of,
        )
        .order_by(IndexMembershipEvent.effective_date, IndexMembershipEvent.id)
    ).all()
    members: dict[str, str | None] = {}
    for event in events:
        if event.event_type == "add":
            members[event.stock_code] = event.stock_name
        else:
            members.pop(event.stock_code, None)
    return members


def materialize_snapshot(db: Session, index_code: str, as_of: date) -> dict[str, Any]:
    """把 as_of 的成分回放结果物化到 stock_universe_snapshots（幂等替换当日）。"""
    members = replay_membership(db, index_code, as_of)
    db.execute(
        delete(StockUniverseSnapshot).where(
            StockUniverseSnapshot.index_code == index_code,
            StockUniverseSnapshot.snapshot_date == as_of,
        )
    )
    for stock_code, stock_name in sorted(members.items()):
        db.add(
            StockUniverseSnapshot(
                index_code=index_code,
                snapshot_date=as_of,
                stock_code=stock_code,
                stock_name=stock_name,
            )
        )
    db.commit()
    return {"index_code": index_code, "snapshot_date": as_of, "members": len(members)}


def get_universe(
    db: Session, index_code: str, as_of: date | None = None
) -> dict[str, Any]:
    """查询指数成分。

    as_of 为空 -> 当前成分（index_constituents）；
    as_of 为今天 -> 当前成分；历史日期 -> 物化快照优先，其次事件实时回放，
    并标注 basis（current/snapshot/replay）供消费方判断可信度。
    """
    today = datetime.now(UTC).date()
    if as_of is None or as_of >= today:
        rows = db.scalars(
            select(IndexConstituent)
            .where(IndexConstituent.index_code == index_code)
            .order_by(IndexConstituent.stock_code)
        ).all()
        return {
            "index_code": index_code,
            "index_name": TRACKED_INDEXES.get(index_code),
            "as_of": as_of or today,
            "basis": "current",
            "members": [
                {"stock_code": row.stock_code, "stock_name": row.stock_name} for row in rows
            ],
        }

    snapshot_rows = db.scalars(
        select(StockUniverseSnapshot)
        .where(
            StockUniverseSnapshot.index_code == index_code,
            StockUniverseSnapshot.snapshot_date == as_of,
        )
        .order_by(StockUniverseSnapshot.stock_code)
    ).all()
    if snapshot_rows:
        return {
            "index_code": index_code,
            "index_name": TRACKED_INDEXES.get(index_code),
            "as_of": as_of,
            "basis": "snapshot",
            "members": [
                {"stock_code": row.stock_code, "stock_name": row.stock_name}
                for row in snapshot_rows
            ],
        }

    members = replay_membership(db, index_code, as_of)
    return {
        "index_code": index_code,
        "index_name": TRACKED_INDEXES.get(index_code),
        "as_of": as_of,
        "basis": "replay",
        "members": [
            {"stock_code": code, "stock_name": name} for code, name in sorted(members.items())
        ],
    }
