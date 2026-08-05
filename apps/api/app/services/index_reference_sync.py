"""官方指数成员/权重与行业分类的持续 PIT 同步。"""

from __future__ import annotations

from datetime import UTC, date, datetime
from hashlib import sha256
from io import BytesIO
import json
from pathlib import Path
from typing import Any, Callable

import pandas as pd
import requests
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import (
    DataSourceSLAState,
    IndexConstituent,
    IndexMembershipEvent,
    QuantDataRecord,
    StockIndustry,
)
from app.services.benchmark_data import _atomic_write
from app.services.research.stock_universe import TRACKED_INDEXES

WEIGHT_URL = (
    "https://oss-ch.csindex.com.cn/static/html/csindex/public/uploads/"
    "file/autofile/closeweight/{code}closeweight.xls"
)
EXPECTED_COUNTS = {"000300": 300, "000905": 500}
SOURCE_POLICIES = {
    "index_membership_weight": {
        "primary_source": "csindex_closeweight",
        "fallback_source": "last_verified_official_snapshot",
        "license_class": "official_public",
        "frequency_minutes": 24 * 60,
        "max_latency_minutes": 3 * 24 * 60,
        "rate_limit": "2 files/day; 3 retries at scheduler layer",
        "owner": "quant-data",
        "failure_mode": "halt_new_orders",
    },
    "industry_classification": {
        "primary_source": "sw2021_structured_membership",
        "fallback_source": "cninfo/eastmoney_current_observation",
        "license_class": "public_and_snapshot",
        "frequency_minutes": 24 * 60,
        "max_latency_minutes": 3 * 24 * 60,
        "rate_limit": "current coverage captured after reference sync",
        "owner": "quant-data",
        "failure_mode": "halt_new_orders",
    },
}


def _sla_state(db: Session, dataset: str) -> DataSourceSLAState:
    policy = SOURCE_POLICIES[dataset]
    state = db.get(DataSourceSLAState, dataset)
    if state is None:
        state = DataSourceSLAState(
            dataset=dataset,
            required=True,
            status="never_run",
            row_count=0,
            consecutive_failures=0,
            degraded=False,
            escalation_level="none",
            detail={},
            **policy,
        )
        db.add(state)
    else:
        for field, value in policy.items():
            setattr(state, field, value)
    return state


def _parse_weights(content: bytes, code: str) -> pd.DataFrame:
    try:
        frame = pd.read_excel(BytesIO(content))
    except Exception as exc:
        raise ValueError(f"{code} 官方权重文件无法解析：{exc}") from exc
    if frame.shape[1] != 10:
        raise ValueError(
            f"{code} 官方权重 schema 变更：预期10列，实际{frame.shape[1]}列"
        )
    frame.columns = [
        "date",
        "index_code",
        "index_name",
        "index_name_en",
        "stock_code",
        "stock_name",
        "stock_name_en",
        "exchange",
        "exchange_en",
        "weight",
    ]
    compact_dates = (
        frame["date"].astype(str).str.strip().str.split(".").str[0]
    )
    parsed_compact = pd.to_datetime(
        compact_dates, format="%Y%m%d", errors="coerce"
    )
    parsed_generic = pd.to_datetime(frame["date"], errors="coerce")
    frame["date"] = parsed_compact.fillna(parsed_generic).dt.date
    frame["index_code"] = (
        frame["index_code"].astype(str).str.split(".").str[0].str.zfill(6)
    )
    frame["stock_code"] = (
        frame["stock_code"].astype(str).str.split(".").str[0].str.zfill(6)
    )
    frame["weight"] = pd.to_numeric(frame["weight"], errors="coerce")
    frame = frame.dropna(subset=["date", "weight"])
    if set(frame["index_code"]) != {code}:
        raise ValueError(f"{code} 官方文件包含其他指数代码")
    if frame["stock_code"].duplicated().any():
        raise ValueError(f"{code} 官方权重存在重复成分")
    if len(frame) != EXPECTED_COUNTS[code]:
        raise ValueError(
            f"{code} 成分数 {len(frame)} != {EXPECTED_COUNTS[code]}"
        )
    total = float(frame["weight"].sum())
    if not 99.5 <= total <= 100.5:
        raise ValueError(f"{code} 权重和 {total:.6f}% 不在允许范围")
    if frame["date"].nunique() != 1:
        raise ValueError(f"{code} 官方权重文件包含多个快照日期")
    return frame


def _download(
    code: str,
    request_get: Callable[..., Any],
) -> tuple[bytes, str]:
    url = WEIGHT_URL.format(code=code)
    response = request_get(url, timeout=60)
    response.raise_for_status()
    content = bytes(response.content)
    if not content:
        raise ValueError(f"{code} 官方权重文件为空")
    return content, url


def sync_official_index_weights(
    db: Session,
    *,
    request_get: Callable[..., Any] = requests.get,
    data_root: Path | None = None,
) -> dict[str, object]:
    """原子更新 300/500 当前成分和权重；任一指数失败则整批不切换。"""
    state = _sla_state(db, "index_membership_weight")
    attempted_at = datetime.now(UTC)
    state.last_attempted_at = attempted_at
    root = data_root or Path(get_settings().research_data_dir)
    parsed: dict[str, tuple[pd.DataFrame, str, str, Path]] = {}
    try:
        for code in TRACKED_INDEXES:
            content, url = _download(code, request_get)
            digest = sha256(content).hexdigest()
            relative = (
                Path("indices")
                / "official_current"
                / f"{code}.{digest}.xls"
            )
            _atomic_write(root / relative, content)
            parsed[code] = (
                _parse_weights(content, code),
                digest,
                url,
                relative,
            )
    except Exception as exc:
        state.status = "failed"
        state.consecutive_failures = int(state.consecutive_failures or 0) + 1
        state.escalation_level = (
            "critical" if state.consecutive_failures >= 3 else "warning"
        )
        state.error = str(exc)
        state.detail = {
            "safe_action": SOURCE_POLICIES[
                "index_membership_weight"
            ]["failure_mode"]
        }
        db.commit()
        raise

    old_members = {
        code: set(
            db.scalars(
                select(IndexConstituent.stock_code).where(
                    IndexConstituent.index_code == code
                )
            ).all()
        )
        for code in TRACKED_INDEXES
    }
    inserted_weights = events = 0
    snapshot_dates: dict[str, str] = {}
    for code, (frame, digest, url, relative) in parsed.items():
        snapshot_date = frame.iloc[0]["date"]
        snapshot_dates[code] = snapshot_date.isoformat()
        current = set(frame["stock_code"])
        available_at = attempted_at
        for stock_code in sorted(current - old_members[code]):
            exists = db.scalar(
                select(IndexMembershipEvent.id).where(
                    IndexMembershipEvent.index_code == code,
                    IndexMembershipEvent.stock_code == stock_code,
                    IndexMembershipEvent.effective_date == snapshot_date,
                    IndexMembershipEvent.event_type == "add",
                )
            )
            if exists is None:
                db.add(
                    IndexMembershipEvent(
                        index_code=code,
                        stock_code=stock_code,
                        event_type="add",
                        effective_date=snapshot_date,
                        source=f"csindex:{digest[:12]}",
                        available_at=available_at,
                    )
                )
                events += 1
        for stock_code in sorted(old_members[code] - current):
            exists = db.scalar(
                select(IndexMembershipEvent.id).where(
                    IndexMembershipEvent.index_code == code,
                    IndexMembershipEvent.stock_code == stock_code,
                    IndexMembershipEvent.effective_date == snapshot_date,
                    IndexMembershipEvent.event_type == "remove",
                )
            )
            if exists is None:
                db.add(
                    IndexMembershipEvent(
                        index_code=code,
                        stock_code=stock_code,
                        event_type="remove",
                        effective_date=snapshot_date,
                        source=f"csindex:{digest[:12]}",
                        available_at=available_at,
                    )
                )
                events += 1
        db.execute(
            delete(IndexConstituent).where(
                IndexConstituent.index_code == code
            )
        )
        for row in frame.to_dict("records"):
            stock_code = str(row["stock_code"])
            db.add(
                IndexConstituent(
                    index_code=code,
                    index_name=TRACKED_INDEXES[code],
                    stock_code=stock_code,
                    stock_name=str(row["stock_name"]),
                    in_date=snapshot_date,
                    source=f"csindex:{digest[:12]}",
                )
            )
            record_code = f"{code}:{stock_code}"
            exists = db.scalar(
                select(QuantDataRecord.id).where(
                    QuantDataRecord.dataset == "index_weight",
                    QuantDataRecord.code == record_code,
                    QuantDataRecord.effective_date == snapshot_date,
                    QuantDataRecord.source_hash == digest,
                )
            )
            if exists is None:
                db.add(
                    QuantDataRecord(
                        dataset="index_weight",
                        code=record_code,
                        effective_date=snapshot_date,
                        available_at=available_at,
                        source=f"csindex:closeweight:{digest[:12]}",
                        source_file=str(relative),
                        source_hash=digest,
                        payload={
                            "index_code": code,
                            "stock_code": stock_code,
                            "weight_percent": float(row["weight"]),
                            "weight_unit": "percent",
                            "composition_method": (
                                "official_constituent_weight"
                            ),
                            "source_url": url,
                        },
                        imported_at=attempted_at,
                    )
                )
                inserted_weights += 1
    state.status = "success"
    state.active_source = "csindex_closeweight"
    state.last_success_at = attempted_at
    state.data_date = min(
        frame.iloc[0]["date"] for frame, *_rest in parsed.values()
    )
    state.schema_hash = sha256(
        json.dumps(
            {
                code: sorted(frame.columns)
                for code, (frame, *_rest) in parsed.items()
            },
            sort_keys=True,
        ).encode()
    ).hexdigest()
    state.row_count = sum(len(frame) for frame, *_rest in parsed.values())
    state.consecutive_failures = 0
    state.degraded = False
    state.escalation_level = "none"
    state.error = None
    state.detail = {
        "snapshot_dates": snapshot_dates,
        "weights_inserted": inserted_weights,
        "membership_events": events,
        "universe_semantics": {
            "production": "dynamic_as_of_signal_date",
            "frozen_experiment": "immutable_candidate_snapshot",
        },
    }
    db.commit()
    return dict(state.detail)


def capture_industry_pit(
    db: Session,
    *,
    observed_on: date | None = None,
) -> dict[str, object]:
    """把当前行业覆盖记为双时态观测；历史精确区间仍由 SW 成员表提供。"""
    target = observed_on or datetime.now(UTC).date()
    now = datetime.now(UTC)
    state = _sla_state(db, "industry_classification")
    state.last_attempted_at = now
    priorities = {
        "stocktoday_sw2021": 100,
        "cninfo": 90,
        "em": 70,
        "ths": 60,
    }
    selected: dict[str, StockIndustry] = {}
    for row in db.scalars(select(StockIndustry)).all():
        previous = selected.get(row.code)
        if previous is None or priorities.get(row.source, 0) > priorities.get(
            previous.source, 0
        ):
            selected[row.code] = row
    universe = set(
        db.scalars(select(IndexConstituent.stock_code).distinct()).all()
    )
    missing = sorted(universe - set(selected))
    if missing:
        state.status = "failed"
        state.consecutive_failures = int(state.consecutive_failures or 0) + 1
        state.escalation_level = "critical"
        state.error = f"当前指数行业缺失 {len(missing)} 只"
        state.detail = {
            "missing_preview": missing[:20],
            "safe_action": "halt_new_orders",
        }
        db.commit()
        raise ValueError(state.error)
    inserted = skipped = 0
    for code in sorted(universe):
        row = selected[code]
        payload = {
            "industry_code": row.industry_code,
            "industry_name": row.industry_name,
            "classification": (
                "SW2021" if row.source == "stocktoday_sw2021" else row.source
            ),
            "effective_date_confidence": (
                "official_interval"
                if row.source == "stocktoday_sw2021"
                else "current_observation"
            ),
        }
        encoded = json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        digest = sha256(encoded.encode()).hexdigest()
        latest = db.scalar(
            select(QuantDataRecord)
            .where(
                QuantDataRecord.dataset == "industry_classification",
                QuantDataRecord.code == code,
            )
            .order_by(QuantDataRecord.available_at.desc())
            .limit(1)
        )
        if latest is not None and latest.source_hash == digest:
            skipped += 1
            continue
        db.add(
            QuantDataRecord(
                dataset="industry_classification",
                code=code,
                effective_date=target,
                available_at=now,
                source=row.source,
                source_file=f"database://stock_industries/{row.id}",
                source_hash=digest,
                payload=payload,
                imported_at=now,
            )
        )
        inserted += 1
    state.status = "success"
    state.active_source = "sw2021_structured_membership"
    state.last_success_at = now
    state.data_date = target
    state.schema_hash = sha256(
        b"classification,industry_code,industry_name,effective_date_confidence"
    ).hexdigest()
    state.row_count = len(universe)
    state.consecutive_failures = 0
    state.degraded = any(
        row.source != "stocktoday_sw2021"
        for row in selected.values()
        if row.code in universe
    )
    state.escalation_level = "warning" if state.degraded else "none"
    state.error = None
    state.detail = {
        "inserted": inserted,
        "skipped": skipped,
        "coverage": len(universe),
        "historical_basis": "SW2021 in_date/out_date",
    }
    db.commit()
    return dict(state.detail)


def source_health(db: Session) -> dict[str, dict[str, object]]:
    now = datetime.now(UTC)
    result: dict[str, dict[str, object]] = {}
    for dataset, policy in SOURCE_POLICIES.items():
        state = db.get(DataSourceSLAState, dataset)
        last = state.last_success_at if state is not None else None
        if last is not None and last.tzinfo is None:
            last = last.replace(tzinfo=UTC)
        overdue = (
            last is None
            or (now - last).total_seconds()
            > int(policy["max_latency_minutes"]) * 60
        )
        status = state.status if state is not None else "never_run"
        result[dataset] = {
            "ready": status in {"success", "degraded"} and not overdue,
            "status": status,
            "degraded": bool(state.degraded) if state is not None else False,
            "overdue": overdue,
            "active_source": (
                state.active_source if state is not None else None
            ),
            "last_success_at": last.isoformat() if last else None,
            "data_date": (
                state.data_date.isoformat()
                if state is not None and state.data_date
                else None
            ),
            "safe_action": policy["failure_mode"],
            "error": state.error if state is not None else "从未成功同步",
        }
    return result
