"""批量数据质量扫描：问题必须落库，原始记录永不就地修改。"""

from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import DataQualityIssue, QuantDataRecord
from app.services.quant_data_governance import record_quality_issue

REQUIRED_PAYLOAD_FIELDS = {
    "daily_basic": ("trade_date", "close", "total_mv"),
    "adj_factor": ("trade_date", "adj_factor"),
    "dividend": ("ex_date",),
    "suspend_d": ("trade_date", "suspend_type"),
    "stk_limit": ("trade_date", "up_limit", "down_limit"),
    "income": ("end_date", "ann_date"),
    "balancesheet": ("end_date", "ann_date", "total_assets", "total_liab"),
    "cashflow": ("end_date", "ann_date"),
    "fina_indicator": ("end_date", "ann_date"),
}
EXTREME_RULES = {
    ("daily_basic", "pe_ttm"): (-1000.0, 3000.0),
    ("daily_basic", "pb"): (-100.0, 300.0),
    ("daily_basic", "total_mv"): (0.0, 1e10),
    ("fina_indicator", "roe"): (-1000.0, 1000.0),
    ("fina_indicator", "debt_to_assets"): (-100.0, 300.0),
    ("balancesheet", "total_assets"): (0.0, 1e16),
    ("balancesheet", "total_liab"): (-1e13, 1e16),
}


def _float(value: object) -> float | None:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _add_issue_once(
    db: Session,
    *,
    dataset: str,
    rule: str,
    detail: str,
    severity: str,
    code: str | None = None,
    field_name: str | None = None,
    original_value: object = None,
    source: str | None = None,
) -> bool:
    existing = db.scalar(
        select(DataQualityIssue.id).where(
            DataQualityIssue.dataset == dataset,
            DataQualityIssue.rule == rule,
            DataQualityIssue.code == code,
            DataQualityIssue.field_name == field_name,
            DataQualityIssue.detail == detail,
            DataQualityIssue.status == "open",
        )
    )
    if existing is not None:
        return False
    record_quality_issue(
        db,
        dataset=dataset,
        rule=rule,
        detail=detail,
        severity=severity,
        code=code,
        field_name=field_name,
        original_value=original_value,
        source=source,
    )
    return True


def scan_quant_records(
    db: Session,
    records: Iterable[QuantDataRecord] | None = None,
) -> dict[str, int]:
    """扫描规范记录的重复、缺失、极值和 schema 变化。"""
    rows = list(
        records
        if records is not None
        else db.scalars(select(QuantDataRecord)).all()
    )
    created = 0
    natural = Counter(
        (
            row.dataset,
            row.code,
            row.effective_date,
            row.available_at,
            row.source,
        )
        for row in rows
    )
    for key, count in natural.items():
        if count <= 1:
            continue
        dataset, code, effective, _available, source = key
        created += _add_issue_once(
            db,
            dataset=dataset,
            code=code,
            rule="duplicate_natural_key",
            detail=f"{code}@{effective} 同源自然键重复 {count} 条",
            severity="error",
            original_value=count,
            source=source,
        )
    for row in rows:
        required = REQUIRED_PAYLOAD_FIELDS.get(row.dataset, ())
        for field in required:
            if row.payload.get(field) in (None, ""):
                created += _add_issue_once(
                    db,
                    dataset=row.dataset,
                    code=row.code,
                    field_name=field,
                    rule="required_field_missing",
                    detail=(
                        f"record={row.id} effective={row.effective_date} "
                        f"缺少必需字段 {field}"
                    ),
                    severity="error",
                    original_value=row.payload.get(field),
                    source=row.source,
                )
        for (dataset, field), (lower, upper) in EXTREME_RULES.items():
            if dataset != row.dataset:
                continue
            value = _float(row.payload.get(field))
            if value is None:
                continue
            if value < lower or value > upper:
                created += _add_issue_once(
                    db,
                    dataset=row.dataset,
                    code=row.code,
                    field_name=field,
                    rule="economic_extreme",
                    detail=(
                        f"record={row.id} {field}={value} 超出"
                        f" [{lower},{upper}]"
                    ),
                    severity="error",
                    original_value=value,
                    source=row.source,
                )
    db.commit()
    return {"scanned": len(rows), "issues_created": created}


def _duck_rows(
    connection: object,
    sql: str,
    params: list[object] | None = None,
) -> list[tuple]:
    return connection.execute(sql, params or []).fetchall()  # type: ignore[union-attr]


def scan_pit_warehouse(
    db: Session,
    *,
    warehouse_path: Path | None = None,
    issue_limit_per_rule: int = 10_000,
) -> dict[str, object]:
    """对实际 PIT 消费层执行批量规则并形成问题闭环。"""
    import duckdb

    path = warehouse_path or Path(get_settings().research_db)
    connection = duckdb.connect(str(path), read_only=True)
    created = 0
    rule_counts: dict[str, int] = {}

    def persist(
        dataset: str,
        rule: str,
        rows: list[tuple],
        *,
        field: str | None = None,
        severity: str = "error",
    ) -> None:
        nonlocal created
        rule_counts[f"{dataset}:{rule}"] = len(rows)
        for code, detail, original in rows[:issue_limit_per_rule]:
            created += _add_issue_once(
                db,
                dataset=dataset,
                code=str(code).split(".")[0] if code else None,
                field_name=field,
                rule=rule,
                detail=str(detail),
                severity=severity,
                original_value=original,
                source="pit_warehouse",
            )

    try:
        specifications = {
            "daily_basic": ("ts_code, trade_date", "ts_code", "trade_date"),
            "adj_factor": ("ts_code, trade_date", "ts_code", "trade_date"),
            "dividend": (
                "ts_code, coalesce(ex_date, ann_date), coalesce(imp_ann_date, ann_date)",
                "ts_code",
                "coalesce(ex_date, ann_date)",
            ),
            "suspend_d": (
                "ts_code, trade_date, suspend_type",
                "ts_code",
                "trade_date",
            ),
            "stk_limit": ("ts_code, trade_date", "ts_code", "trade_date"),
            "income": (
                "ts_code, end_date, ann_date, report_type",
                "ts_code",
                "end_date",
            ),
            "balancesheet": (
                "ts_code, end_date, ann_date, report_type",
                "ts_code",
                "end_date",
            ),
            "cashflow": (
                "ts_code, end_date, ann_date, report_type",
                "ts_code",
                "end_date",
            ),
            "fina_indicator": (
                "ts_code, end_date, ann_date",
                "ts_code",
                "end_date",
            ),
        }
        for dataset, (keys, code_field, date_field) in specifications.items():
            duplicates = _duck_rows(
                connection,
                f"""
                select min({code_field}),
                       concat('PIT自然键重复: ', cast(min({date_field}) as varchar),
                              ' count=', cast(count(*) as varchar)),
                       count(*)
                  from pit_{dataset}
                 group by {keys}
                having count(*) > 1
                """,
            )
            persist(dataset, "duplicate_natural_key", duplicates)

        missing_specs = {
            "daily_basic": "ts_code is null or trade_date is null or close is null",
            "adj_factor": "ts_code is null or trade_date is null or adj_factor is null",
            "dividend": "ts_code is null or coalesce(ex_date, ann_date) is null",
            "suspend_d": "ts_code is null or trade_date is null or suspend_type is null",
            "stk_limit": (
                "ts_code is null or trade_date is null or "
                "up_limit is null or down_limit is null"
            ),
            "income": "ts_code is null or end_date is null or ann_date is null",
            "balancesheet": (
                "ts_code is null or end_date is null or ann_date is null "
                "or total_assets is null or total_liab is null"
            ),
            "cashflow": "ts_code is null or end_date is null or ann_date is null",
            "fina_indicator": "ts_code is null or end_date is null or ann_date is null",
        }
        for dataset, predicate in missing_specs.items():
            rows = _duck_rows(
                connection,
                f"""
                select ts_code,
                       concat('必需字段缺失 effective=', cast(effective_date as varchar)),
                       to_json(struct_pack(source_file := source_file))
                  from pit_{dataset}
                 where {predicate}
                 limit {issue_limit_per_rule}
                """,
            )
            persist(dataset, "required_field_missing", rows)

        financial_equation = _duck_rows(
            connection,
            f"""
            select ts_code,
                   concat('资产负债表不平 effective=', cast(effective_date as varchar),
                          ' relative_error=',
                          cast(abs(total_assets-total_liab_hldr_eqy)
                               / greatest(abs(total_assets),1) as varchar)),
                   to_json(struct_pack(
                       total_assets := total_assets,
                       total_liab_hldr_eqy := total_liab_hldr_eqy))
              from pit_balancesheet
             where total_assets is not null
               and total_liab_hldr_eqy is not null
               and abs(total_assets-total_liab_hldr_eqy)
                   / greatest(abs(total_assets),1) > 0.01
             limit {issue_limit_per_rule}
            """,
        )
        persist(
            "balancesheet",
            "accounting_equation_assets",
            financial_equation,
        )
        cash_equation = _duck_rows(
            connection,
            f"""
            select ts_code,
                   concat('现金勾稽失败 effective=', cast(effective_date as varchar)),
                   to_json(struct_pack(
                       begin_cash := c_cash_equ_beg_period,
                       net_change := n_incr_cash_cash_equ,
                       end_cash := c_cash_equ_end_period))
              from pit_cashflow
             where c_cash_equ_beg_period is not null
               and n_incr_cash_cash_equ is not null
               and c_cash_equ_end_period is not null
               and abs(c_cash_equ_beg_period+n_incr_cash_cash_equ
                       -c_cash_equ_end_period)
                   / greatest(abs(c_cash_equ_end_period),1) > 0.02
             limit {issue_limit_per_rule}
            """,
        )
        persist("cashflow", "accounting_equation_cash", cash_equation)

        # OCF/利润在接近盈亏平衡点会发散；问题必须落账，正式质量因子
        # 使用 (OCF-NI)/总资产，而不是仅在横截面末端缩尾掩盖。
        small_denominators = _duck_rows(
            connection,
            f"""
            with income_latest as (
                select ts_code, end_date, n_income_attr_p,
                       row_number() over (
                         partition by ts_code, end_date order by available_date desc
                       ) as rn
                  from pit_income
            ),
            cash_latest as (
                select ts_code, end_date, n_cashflow_act,
                       row_number() over (
                         partition by ts_code, end_date order by available_date desc
                       ) as rn
                  from pit_cashflow
            ),
            balance_latest as (
                select ts_code, end_date, total_assets,
                       row_number() over (
                         partition by ts_code, end_date order by available_date desc
                       ) as rn
                  from pit_balancesheet
            )
            select i.ts_code,
                   concat('利润分母不稳定 end_date=', cast(i.end_date as varchar),
                          ' profit=', cast(i.n_income_attr_p as varchar),
                          ' assets=', cast(b.total_assets as varchar)),
                   i.n_income_attr_p
              from income_latest i
              join cash_latest c
                on c.ts_code=i.ts_code and c.end_date=i.end_date and c.rn=1
              join balance_latest b
                on b.ts_code=i.ts_code and b.end_date=i.end_date and b.rn=1
             where i.rn=1
               and i.n_income_attr_p is not null
               and c.n_cashflow_act is not null
               and b.total_assets > 0
               and abs(i.n_income_attr_p)
                   < greatest(1000000, abs(b.total_assets)*0.005)
             limit {issue_limit_per_rule}
            """,
        )
        persist(
            "factor_input",
            "unstable_financial_denominator",
            small_denominators,
            field="ocf_to_profit",
        )

        extreme_specs = {
            ("daily_basic", "pe_ttm"): "abs(pe_ttm) > 3000",
            ("daily_basic", "pb"): "abs(pb) > 300",
            ("daily_basic", "total_mv"): "total_mv <= 0",
            ("fina_indicator", "roe"): "abs(roe) > 1000",
            ("fina_indicator", "debt_to_assets"): (
                "debt_to_assets < -100 or debt_to_assets > 300"
            ),
        }
        for (dataset, field), predicate in extreme_specs.items():
            rows = _duck_rows(
                connection,
                f"""
                select ts_code,
                       concat('经济极值 {field}=', cast({field} as varchar),
                              ' effective=', cast(effective_date as varchar)),
                       {field}
                  from pit_{dataset}
                 where {field} is not null and ({predicate})
                 limit {issue_limit_per_rule}
                """,
            )
            persist(
                dataset,
                "economic_extreme",
                rows,
                field=field,
            )

        for dataset, fields in {
            "daily_basic": ("pe_ttm", "pb", "dv_ttm", "total_mv"),
            "fina_indicator": ("roe", "grossprofit_margin", "debt_to_assets"),
        }.items():
            for field in fields:
                count, distinct = _duck_rows(
                    connection,
                    f"""
                    select count({field}), count(distinct {field})
                      from pit_{dataset}
                    """,
                )[0]
                if count >= 100 and distinct <= 1:
                    persist(
                        dataset,
                        "constant_field",
                        [
                            (
                                None,
                                f"{field} 非空 {count} 行但唯一值仅 {distinct}",
                                distinct,
                            )
                        ],
                        field=field,
                    )
        db.commit()
        return {
            "status": "success",
            "scanned_at": datetime.now(UTC).isoformat(),
            "issues_created": created,
            "rule_counts": rule_counts,
        }
    finally:
        connection.close()
