"""停牌、涨跌停、分红和名称变更的可持续数据供应与 SLA 门禁。"""

from __future__ import annotations

import hashlib
import json
import math
import re
import subprocess
import sys
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
from typing import Callable, Iterable

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import (
    DataFieldProvenance,
    DataSourceSLAState,
    IndexConstituent,
    QuantDataRecord,
    StockDailyBar,
    StockMaster,
    StockPaperPosition,
)
from app.services.price_limit_rules import price_limit_rule
from app.services.quant_data_governance import SOURCE_PRIORITY


@dataclass(frozen=True)
class SourcePolicy:
    dataset: str
    primary_source: str
    fallback_source: str
    license_class: str
    frequency_minutes: int
    max_latency_minutes: int
    rate_limit: str
    owner: str
    failure_mode: str


POLICIES: dict[str, SourcePolicy] = {
    "suspend_d": SourcePolicy(
        "suspend_d",
        "baidu_trade_notice",
        "eastmoney_quote_absence",
        "public",
        24 * 60,
        18 * 60,
        "1 request/trading-day; exponential backoff",
        "quant-data",
        "halt_new_orders",
    ),
    "stk_limit": SourcePolicy(
        "stk_limit",
        "eastmoney_close_snapshot",
        "exchange_rule_derived",
        "public",
        24 * 60,
        4 * 60,
        "1 paginated snapshot/trading-day; exponential backoff",
        "quant-data",
        "reduce_only",
    ),
    "dividend": SourcePolicy(
        "dividend",
        "baidu_trade_notice",
        "cninfo_announcement",
        "public",
        24 * 60,
        36 * 60,
        "1 request/trading-day; per-stock fallback throttled",
        "quant-data",
        "halt_new_orders",
    ),
    "namechange": SourcePolicy(
        "namechange",
        "exchange_current_master_diff",
        "sina_name_history",
        "public",
        24 * 60,
        36 * 60,
        "1 market master/trading-day; changed stocks only",
        "quant-data",
        "halt_new_orders",
    ),
}

PROVIDER_CONTINUITY = {
    "tushare_historical_snapshot": {
        "authorization": "短期付费授权，已失效，仅保留不可变历史快照",
        "credential_storage": "环境/本地受限配置；禁止进入代码、日志和 API",
        "frequency": "不再在线调用",
        "rate_limit": "not_applicable",
        "owner": "quant-data",
        "fallback": "公开源 + 已冻结 PIT archive",
    },
    "baidu_trade_notice": {
        "authorization": "公开网页结构化接口",
        "frequency": "每交易日",
        "rate_limit": "单日单接口一次，失败指数退避",
        "owner": "quant-data",
        "fallback": "交易所/巨潮/行情缺席保守推断",
    },
    "csindex_closeweight": {
        "authorization": "中证指数官网公开文件",
        "frequency": "每交易日检查",
        "rate_limit": "每指数每日一次",
        "owner": "quant-data",
        "fallback": "最后一个已验证官方快照；停止新信号",
    },
    "exchange_and_cninfo": {
        "authorization": "交易所/法定披露公开源",
        "frequency": "每交易日",
        "rate_limit": "批量优先、持仓逐股回退",
        "owner": "quant-data",
        "fallback": "新浪/东方财富公开源；冲突进入人工复核",
    },
}

Fetcher = Callable[[Session, date], list[dict[str, object]]]


def _json_value(value: object) -> object:
    if value is None:
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if hasattr(value, "item"):
        return _json_value(value.item())  # type: ignore[union-attr]
    return value


def _records(frame: object) -> list[dict[str, object]]:
    rows = getattr(frame, "to_dict")("records")
    return [
        {str(key): _json_value(value) for key, value in row.items()} for row in rows
    ]


def _akshare_calls(
    calls: list[tuple[str, dict[str, object]]],
    *,
    timeout_seconds: int = 90,
) -> list[dict[str, object]]:
    """在隔离进程运行不可信 SDK，崩溃、退出和超时都转成普通失败。"""
    request = {
        "calls": [
            {"function": function, "kwargs": kwargs} for function, kwargs in calls
        ]
    }
    try:
        completed = subprocess.run(
            [sys.executable, "-m", "app.services.akshare_bridge"],
            input=json.dumps(request, ensure_ascii=False),
            text=True,
            capture_output=True,
            timeout=timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"AkShare 子进程超过 {timeout_seconds} 秒") from exc
    marker = "__AKSHARE_RESULT__"
    payload = next(
        (
            line[len(marker) :]
            for line in reversed(completed.stdout.splitlines())
            if line.startswith(marker)
        ),
        None,
    )
    if completed.returncode != 0 or payload is None:
        detail = (completed.stderr or completed.stdout).strip()[-1000:]
        raise RuntimeError(f"AkShare 子进程异常退出({completed.returncode})：{detail}")
    rows = json.loads(payload)
    if not isinstance(rows, list):
        raise RuntimeError("AkShare 子进程返回类型不是 list")
    return rows


def _number(text: object) -> float | None:
    match = re.search(r"-?\d+(?:\.\d+)?", str(text or ""))
    return float(match.group()) if match else None


def _fetch_suspend_baidu(_db: Session, as_of: date) -> list[dict[str, object]]:
    rows = _akshare_calls(
        [
            (
                "news_trade_notify_suspend_baidu",
                {"date": as_of.strftime("%Y%m%d")},
            )
        ]
    )
    return [
        {
            "ts_code": str(row.get("股票代码") or "").zfill(6),
            "trade_date": str(row.get("停牌时间") or as_of.isoformat()),
            "resume_date": row.get("复牌时间"),
            "suspend_type": "S",
            "reason": row.get("停牌事项说明"),
            "announcement_date": row.get("公告日期"),
        }
        for row in rows
        if str(row.get("交易所代码") or "").upper() in {"SH", "SZ", "BJ"}
    ]


def _fetch_dividend_baidu(_db: Session, as_of: date) -> list[dict[str, object]]:
    rows = _akshare_calls(
        [
            (
                "news_trade_notify_dividend_baidu",
                {"date": as_of.strftime("%Y%m%d")},
            )
        ]
    )
    result: list[dict[str, object]] = []
    for row in rows:
        if str(row.get("交易所") or "").upper() not in {"SH", "SZ", "BJ"}:
            continue
        result.append(
            {
                "ts_code": str(row.get("股票代码") or "").zfill(6),
                "ann_date": as_of.isoformat(),
                "ex_date": row.get("除权日") or as_of.isoformat(),
                "cash_div": _number(row.get("分红")),
                "stk_div": _number(row.get("送股")),
                "stk_bo_rate": _number(row.get("转增")),
                "status": "implemented",
                "raw_description": {
                    "dividend": row.get("分红"),
                    "stock_dividend": row.get("送股"),
                    "capitalization": row.get("转增"),
                    "report_period": row.get("报告期"),
                },
            }
        )
    return result


def _fetch_current_names(db: Session, as_of: date) -> list[dict[str, object]]:
    existing = {row.code: row.name for row in db.scalars(select(StockMaster)).all()}
    rows = _akshare_calls(
        [
            ("stock_info_sh_name_code", {"symbol": "主板A股"}),
            ("stock_info_sh_name_code", {"symbol": "科创板"}),
            ("stock_info_sz_name_code", {"symbol": "A股列表"}),
        ]
    )
    result: list[dict[str, object]] = []
    for row in rows:
        code = str(row.get("证券代码") or row.get("A股代码") or "").zfill(6)
        name = str(row.get("证券简称") or row.get("A股简称") or "").strip()
        old_name = existing.get(code)
        if code and name and old_name and old_name != name:
            result.append(
                {
                    "ts_code": code,
                    "start_date": as_of.isoformat(),
                    "ann_date": as_of.isoformat(),
                    "name": name,
                    "old_name": old_name,
                    "change_reason": "公开市场证券主表名称发生变化",
                }
            )
    return result


def _listing_sessions(db: Session, code: str, as_of: date) -> int | None:
    metadata = db.get(StockDailyBar, code)
    if (
        metadata is None
        or metadata.last_trade_date is None
        or metadata.last_trade_date > as_of
    ):
        return None
    return int(metadata.rows) if metadata.rows else None


def _round_price(value: float) -> float:
    return float(Decimal(str(value)).quantize(Decimal("0.01"), ROUND_HALF_UP))


def _limits_from_quote_rows(
    db: Session,
    as_of: date,
    rows: list[dict[str, object]],
) -> list[dict[str, object]]:
    result: list[dict[str, object]] = []
    for row in rows:
        code = str(row.get("代码") or "").zfill(6)
        previous = _number(row.get("昨收"))
        name = str(row.get("名称") or "")
        if not code or previous is None or previous <= 0:
            continue
        try:
            rule = price_limit_rule(
                code,
                as_of,
                st="ST" in name.upper(),
                listing_session=_listing_sessions(db, code, as_of),
                delisting_period="退" in name,
            )
        except ValueError:
            continue
        result.append(
            {
                "ts_code": code,
                "trade_date": as_of.isoformat(),
                "pre_close": previous,
                "up_limit": (
                    _round_price(previous * (1 + rule.upper_limit))
                    if rule.upper_limit is not None
                    else None
                ),
                "down_limit": (
                    _round_price(previous * (1 - rule.lower_limit))
                    if rule.lower_limit is not None
                    else None
                ),
                "rule_version": rule.version,
                "no_limit_reason": rule.no_limit_reason,
            }
        )
    return result


def _fetch_limits_eastmoney(db: Session, as_of: date) -> list[dict[str, object]]:
    return _limits_from_quote_rows(
        db,
        as_of,
        _akshare_calls([("stock_zh_a_spot_em", {})]),
    )


def _fetch_limits_derived(db: Session, as_of: date) -> list[dict[str, object]]:
    from app.services.stock_repository import load_repository

    codes = list(db.scalars(select(IndexConstituent.stock_code).distinct()).all())
    repository = load_repository(db)
    if repository is None or not codes:
        raise RuntimeError("没有可用于推导涨跌停价的行情仓储或指数股票池")
    bars = repository.daily_bars(
        codes,
        start=as_of - timedelta(days=14),
        end=as_of - timedelta(days=1),
    )
    latest: dict[str, object] = {}
    for bar in bars:
        previous = latest.get(bar.code)
        if previous is None or previous.trade_date < bar.trade_date:  # type: ignore[union-attr]
            latest[bar.code] = bar
    names = {
        code: name
        for code, name in db.execute(
            select(StockMaster.code, StockMaster.name).where(
                StockMaster.code.in_(codes)
            )
        ).all()
    }
    quotes = [
        {
            "代码": code,
            "昨收": float(bar.close),  # type: ignore[union-attr]
            "名称": names.get(code, ""),
        }
        for code, bar in latest.items()
        if getattr(bar, "close", None) is not None
    ]
    if not quotes:
        raise RuntimeError("没有可用于推导涨跌停价的前收盘行情")
    return _limits_from_quote_rows(db, as_of, quotes)


def _tracked_codes(db: Session) -> list[str]:
    held = list(
        db.scalars(
            select(StockPaperPosition.code)
            .where(StockPaperPosition.shares > 0)
            .distinct()
        ).all()
    )
    return sorted(set(held))


def _fetch_suspend_quote_absence(db: Session, as_of: date) -> list[dict[str, object]]:
    tracked = set(db.scalars(select(IndexConstituent.stock_code).distinct()).all())
    quoted = {
        str(row.get("代码") or "").zfill(6)
        for row in _akshare_calls([("stock_zh_a_spot_em", {})])
    }
    if not quoted:
        raise RuntimeError("公开行情快照为空，不能据此推断停牌")
    return [
        {
            "ts_code": code,
            "trade_date": as_of.isoformat(),
            "suspend_type": "S",
            "reason": "收盘公开行情缺席，备用源保守判定并等待公告确认",
        }
        for code in sorted(tracked - quoted)
    ]


def _fetch_dividend_cninfo(db: Session, as_of: date) -> list[dict[str, object]]:
    result: list[dict[str, object]] = []
    for code in _tracked_codes(db):
        for row in _akshare_calls([("stock_dividend_cninfo", {"symbol": code})]):
            ann_date = str(row.get("实施方案公告日期") or "")
            if ann_date[:10] != as_of.isoformat():
                continue
            result.append(
                {
                    "ts_code": code,
                    "ann_date": ann_date,
                    "ex_date": row.get("除权日") or ann_date,
                    "pay_date": row.get("派息日"),
                    "cash_div": row.get("派息比例"),
                    "stk_div": row.get("送股比例"),
                    "stk_bo_rate": row.get("转增比例"),
                    "status": "implemented",
                }
            )
    if not _tracked_codes(db):
        raise RuntimeError("备用源仅保护持仓证券，当前没有持仓可核对")
    return result


def _fetch_name_history_sina(db: Session, as_of: date) -> list[dict[str, object]]:
    result: list[dict[str, object]] = []
    current_names = {
        row.code: row.name
        for row in db.scalars(
            select(StockMaster).where(StockMaster.code.in_(_tracked_codes(db)))
        ).all()
    }
    if not current_names:
        raise RuntimeError("备用源仅保护持仓证券，当前没有持仓可核对")
    for code, current in current_names.items():
        rows = _akshare_calls([("stock_info_change_name", {"symbol": code})])
        if not rows:
            continue
        newest = str(rows[-1].get("name") or "").strip()
        if newest and newest != current:
            result.append(
                {
                    "ts_code": code,
                    "ann_date": as_of.isoformat(),
                    "start_date": as_of.isoformat(),
                    "name": newest,
                    "old_name": current,
                    "change_reason": "新浪曾用名备用源检测到名称变化",
                }
            )
    return result


PRIMARY_FETCHERS: dict[str, Fetcher] = {
    "suspend_d": _fetch_suspend_baidu,
    "stk_limit": _fetch_limits_eastmoney,
    "dividend": _fetch_dividend_baidu,
    "namechange": _fetch_current_names,
}

FALLBACK_FETCHERS: dict[str, Fetcher] = {
    "suspend_d": _fetch_suspend_quote_absence,
    "stk_limit": _fetch_limits_derived,
    "dividend": _fetch_dividend_cninfo,
    "namechange": _fetch_name_history_sina,
}

REQUIRED_FIELDS = {
    "suspend_d": {"ts_code", "trade_date", "suspend_type"},
    "stk_limit": {"ts_code", "trade_date", "up_limit", "down_limit"},
    "dividend": {"ts_code", "ex_date"},
    "namechange": {"ts_code", "start_date", "name"},
}


def _state(db: Session, policy: SourcePolicy) -> DataSourceSLAState:
    state = db.get(DataSourceSLAState, policy.dataset)
    values = asdict(policy)
    if state is None:
        state = DataSourceSLAState(
            dataset=policy.dataset,
            required=True,
            status="never_run",
            row_count=0,
            consecutive_failures=0,
            degraded=False,
            escalation_level="none",
            detail={},
            **{key: value for key, value in values.items() if key != "dataset"},
        )
        db.add(state)
    else:
        for key, value in values.items():
            if key != "dataset":
                setattr(state, key, value)
    return state


def initialize_policies(db: Session) -> None:
    for policy in POLICIES.values():
        _state(db, policy)
    db.commit()


def _record_date(dataset: str, row: dict[str, object], default: date) -> date:
    for field in {
        "suspend_d": ("trade_date",),
        "stk_limit": ("trade_date",),
        "dividend": ("ex_date", "ann_date"),
        "namechange": ("start_date", "ann_date"),
    }[dataset]:
        value = str(row.get(field) or "")
        try:
            return date.fromisoformat(value[:10])
        except ValueError:
            continue
    return default


def _persist_rows(
    db: Session,
    *,
    dataset: str,
    source: str,
    rows: list[dict[str, object]],
    as_of: date,
    imported_at: datetime,
) -> tuple[int, int]:
    inserted = skipped = 0
    priority = SOURCE_PRIORITY.get(
        "eastmoney" if "eastmoney" in source else "akshare", 50
    )
    for raw in rows:
        payload = {key: _json_value(value) for key, value in raw.items()}
        missing = REQUIRED_FIELDS[dataset] - set(payload)
        if missing:
            raise ValueError(f"{dataset} schema 缺少字段：{','.join(sorted(missing))}")
        canonical = json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        checksum = hashlib.sha256(canonical.encode()).hexdigest()
        code = str(payload["ts_code"]).split(".")[0].zfill(6)
        effective = _record_date(dataset, payload, as_of)
        exists = db.scalar(
            select(QuantDataRecord.id).where(
                QuantDataRecord.dataset == dataset,
                QuantDataRecord.code == code,
                QuantDataRecord.effective_date == effective,
                QuantDataRecord.source_hash == checksum,
            )
        )
        if exists is not None:
            skipped += 1
            continue
        record = QuantDataRecord(
            dataset=dataset,
            code=code,
            effective_date=effective,
            available_at=imported_at,
            source=source,
            source_file=f"api://{source}/{dataset}/{as_of.isoformat()}",
            source_hash=checksum,
            payload=payload,
            imported_at=imported_at,
        )
        db.add(record)
        db.flush()
        for field_name, value in payload.items():
            db.add(
                DataFieldProvenance(
                    record_id=record.id,
                    field_name=field_name,
                    source=source,
                    source_priority=priority,
                    quality_status="missing" if value is None else "valid",
                    original_value=(
                        json.dumps(value, ensure_ascii=False)
                        if value is not None
                        else None
                    ),
                    normalized_value=(
                        json.dumps(value, ensure_ascii=False)
                        if value is not None
                        else None
                    ),
                )
            )
        inserted += 1
    return inserted, skipped


def _schema_hash(rows: list[dict[str, object]]) -> str:
    fields = sorted({field for row in rows for field in row})
    return hashlib.sha256(json.dumps(fields, ensure_ascii=False).encode()).hexdigest()


def provider_catalog(db: Session) -> dict[str, object]:
    states = {
        row.dataset: {
            "status": row.status,
            "primary_source": row.primary_source,
            "fallback_source": row.fallback_source,
            "license_class": row.license_class,
            "frequency_minutes": row.frequency_minutes,
            "max_latency_minutes": row.max_latency_minutes,
            "rate_limit": row.rate_limit,
            "owner": row.owner,
            "failure_mode": row.failure_mode,
            "consecutive_failures": row.consecutive_failures,
            "escalation_level": row.escalation_level,
        }
        for row in db.scalars(select(DataSourceSLAState)).all()
    }
    return {
        "providers": PROVIDER_CONTINUITY,
        "datasets": states,
        "global_safe_action": (
            "halt_new_orders"
            if any(
                item["status"] in {"failed", "never_run"}
                for item in states.values()
                if item["failure_mode"] == "halt_new_orders"
            )
            else "normal"
        ),
    }


def refresh_execution_references(
    db: Session,
    *,
    as_of: date | None = None,
    datasets: Iterable[str] | None = None,
    primary_fetchers: dict[str, Fetcher] | None = None,
    fallback_fetchers: dict[str, Fetcher] | None = None,
) -> dict[str, object]:
    """刷新四个必需数据集；单数据集失败不会掩盖其他数据集结果。"""
    target = as_of or datetime.now(UTC).date()
    selected = tuple(dict.fromkeys(datasets or POLICIES))
    unknown = sorted(set(selected) - set(POLICIES))
    if unknown:
        raise ValueError("未知执行参考数据集：" + ",".join(unknown))
    primary = primary_fetchers or PRIMARY_FETCHERS
    fallback = fallback_fetchers or FALLBACK_FETCHERS
    now = datetime.now(UTC)
    results: dict[str, object] = {}
    for dataset in selected:
        policy = POLICIES[dataset]
        state = _state(db, policy)
        state.last_attempted_at = now
        errors: list[str] = []
        rows: list[dict[str, object]] | None = None
        active_source: str | None = None
        degraded = False
        try:
            rows = primary[dataset](db, target)
            active_source = policy.primary_source
        except BaseException as exc:  # noqa: BLE001
            if isinstance(exc, KeyboardInterrupt):
                raise
            errors.append(f"主源 {policy.primary_source}: {exc}")
            try:
                rows = fallback[dataset](db, target)
                active_source = policy.fallback_source
                degraded = True
            except BaseException as fallback_exc:  # noqa: BLE001
                if isinstance(fallback_exc, KeyboardInterrupt):
                    raise
                errors.append(f"备用源 {policy.fallback_source}: {fallback_exc}")
        if rows is None:
            state.status = "failed"
            state.active_source = None
            state.consecutive_failures = int(state.consecutive_failures or 0) + 1
            state.degraded = False
            state.escalation_level = (
                "critical" if state.consecutive_failures >= 3 else "warning"
            )
            state.error = "；".join(errors)
            state.detail = {"errors": errors, "safe_action": policy.failure_mode}
            results[dataset] = {
                "status": "failed",
                "errors": errors,
                "safe_action": policy.failure_mode,
            }
            db.commit()
            continue
        if dataset == "stk_limit":
            expected = int(db.scalar(select(func.count(IndexConstituent.id))) or 0)
            if expected and len(rows) < int(expected * 0.95):
                errors.append(f"批量缺失：stk_limit {len(rows)}/{expected} 低于95%")
                rows = None
        if rows is None:
            state.status = "failed"
            state.active_source = None
            state.consecutive_failures = int(state.consecutive_failures or 0) + 1
            state.degraded = False
            state.escalation_level = "critical"
            state.error = "；".join(errors)
            state.detail = {
                "errors": errors,
                "safe_action": policy.failure_mode,
            }
            results[dataset] = {
                "status": "failed",
                "errors": errors,
                "safe_action": policy.failure_mode,
            }
            db.commit()
            continue
        # 空结果只能证明“当天没有事件”，无法证明上游完整字段集合。
        # 因此首次空结果不建立 schema 指纹；从空结果转为非空时允许采纳
        # 第一个真实 schema，避免把合法的可选字段误报为接口漂移。
        new_schema_hash = _schema_hash(rows) if rows else state.schema_hash
        if (
            state.schema_hash
            and rows
            and int(state.row_count or 0) > 0
            and state.schema_hash != new_schema_hash
        ):
            state.status = "failed"
            state.consecutive_failures = int(state.consecutive_failures or 0) + 1
            state.escalation_level = "critical"
            state.error = "接口 schema 与上次成功版本不一致"
            state.detail = {
                "previous_schema_hash": state.schema_hash,
                "observed_schema_hash": new_schema_hash,
                "safe_action": policy.failure_mode,
            }
            results[dataset] = {
                "status": "failed",
                "errors": [state.error],
                "safe_action": policy.failure_mode,
            }
            db.commit()
            continue
        try:
            inserted, skipped = _persist_rows(
                db,
                dataset=dataset,
                source=str(active_source),
                rows=rows,
                as_of=target,
                imported_at=now,
            )
        except Exception as exc:  # noqa: BLE001
            db.rollback()
            state = _state(db, policy)
            state.last_attempted_at = now
            state.status = "failed"
            state.consecutive_failures = int(state.consecutive_failures or 0) + 1
            state.escalation_level = "critical"
            state.error = f"schema/persist: {exc}"
            state.detail = {
                "errors": errors + [str(exc)],
                "safe_action": policy.failure_mode,
            }
            db.commit()
            results[dataset] = {
                "status": "failed",
                "errors": errors + [str(exc)],
                "safe_action": policy.failure_mode,
            }
            continue
        state.status = "degraded" if degraded else "success"
        state.active_source = active_source
        state.last_success_at = now
        state.data_date = target
        state.schema_hash = new_schema_hash
        state.row_count = len(rows)
        state.consecutive_failures = 0
        state.degraded = degraded
        state.escalation_level = "warning" if degraded else "none"
        state.error = "；".join(errors) if errors else None
        state.detail = {
            "inserted": inserted,
            "skipped": skipped,
            "errors": errors,
            "safe_action": policy.failure_mode,
        }
        db.commit()
        results[dataset] = {
            "status": state.status,
            "source": active_source,
            "rows": len(rows),
            "inserted": inserted,
            "skipped": skipped,
            "errors": errors,
        }
    return {"as_of": target.isoformat(), "datasets": results}


def sla_health(
    db: Session,
    *,
    now: datetime | None = None,
) -> dict[str, dict[str, object]]:
    """返回交易前门禁所需的逐数据集 SLA 状态。"""
    current = now or datetime.now(UTC)
    result: dict[str, dict[str, object]] = {}
    for dataset, policy in POLICIES.items():
        state = db.get(DataSourceSLAState, dataset)
        last_success = state.last_success_at if state is not None else None
        if last_success is not None and last_success.tzinfo is None:
            last_success = last_success.replace(tzinfo=UTC)
        overdue = last_success is None or current - last_success > timedelta(
            minutes=policy.max_latency_minutes
        )
        status = state.status if state is not None else "never_run"
        ready = status in {"success", "degraded"} and not overdue
        result[dataset] = {
            "ready": ready,
            "status": status,
            "overdue": overdue,
            "degraded": bool(state.degraded) if state is not None else False,
            "active_source": state.active_source if state is not None else None,
            "primary_source": policy.primary_source,
            "fallback_source": policy.fallback_source,
            "frequency_minutes": policy.frequency_minutes,
            "max_latency_minutes": policy.max_latency_minutes,
            "last_success_at": (
                last_success.isoformat() if last_success is not None else None
            ),
            "data_date": (
                state.data_date.isoformat()
                if state is not None and state.data_date is not None
                else None
            ),
            "row_count": state.row_count if state is not None else 0,
            "escalation_level": (
                state.escalation_level if state is not None else "critical"
            ),
            "safe_action": policy.failure_mode,
            "error": state.error if state is not None else "从未成功同步",
        }
    return result
