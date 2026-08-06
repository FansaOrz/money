"""对价格、财务、估值、行业和公司行为执行跨源批量复核。"""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

import duckdb
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import (
    IndexConstituent,
    QuantDataRecord,
    StockFinancialIndicator,
    StockIndustry,
    StockValuation,
)
from app.services.source_reconciliation import reconcile_field


def _pit_latest(
    warehouse: duckdb.DuckDBPyConnection,
    dataset: str,
    fields: tuple[str, ...],
    as_of: date,
) -> dict[str, tuple]:
    columns = ", ".join(fields)
    rows = warehouse.execute(
        f"""
        select ts_code, {columns}
          from pit_{dataset}
         where effective_date <= ?
         qualify row_number() over (
             partition by ts_code
             order by effective_date desc, available_date desc,
                      ingested_at desc
         ) = 1
        """,
        [as_of],
    ).fetchall()
    return {str(row[0]).split(".")[0]: tuple(row[1:]) for row in rows}


def _source_root(source: str) -> str:
    normalized = str(source or "").split(":")[0]
    aliases = {
        "stocktoday": "tushare",
        "stocktoday_sw2021": "stocktoday_sw2021",
        "tencent_close": "tencent",
        "cninfo_profile": "cninfo",
        "baidu_trade_notice": "baidu",
    }
    return aliases.get(normalized, normalized)


def _unique_source_candidates(
    candidates: list[tuple[str, object]],
) -> list[tuple[str, object]]:
    """同一根来源只保留一项，禁止把导入副本伪装成跨源验证。"""
    result: list[tuple[str, object]] = []
    seen: set[str] = set()
    for source, value in candidates:
        root = _source_root(source)
        if root in seen:
            continue
        seen.add(root)
        result.append((root, value))
    return result


def _as_date(value: object) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


def run_cross_source_reconciliation(
    db: Session,
    *,
    as_of: date,
    warehouse_path: Path | None = None,
) -> dict[str, object]:
    universe = sorted(
        set(db.scalars(select(IndexConstituent.stock_code).distinct()).all())
    )
    path = warehouse_path or Path(get_settings().research_db)
    warehouse = duckdb.connect(str(path), read_only=True)
    decisions = 0
    statuses: defaultdict[str, int] = defaultdict(int)

    def decide(**kwargs) -> None:
        nonlocal decisions
        decision = reconcile_field(db, commit=False, **kwargs)
        decisions += 1
        statuses[decision.status] += 1

    try:
        daily = _pit_latest(
            warehouse,
            "daily_basic",
            ("effective_date", "close", "pe_ttm", "pb"),
            as_of,
        )
        financial = _pit_latest(
            warehouse,
            "fina_indicator",
            ("effective_date", "roe"),
            as_of,
        )
        # 仓储可能优先挂载同一 Tushare 快照，StockBar 又不携带来源；
        # 因此不能把仓储返回值伪标为 Sina 形成虚假的“双源一致”。
        valuation_rows = db.scalars(
            select(StockValuation).where(
                StockValuation.code.in_(universe),
                StockValuation.trade_date <= as_of,
                StockValuation.trade_date >= as_of - timedelta(days=10),
                StockValuation.indicator.in_(("pe_ttm", "pb")),
            )
        ).all()
        valuations: dict[tuple[str, str, date], StockValuation] = {}
        for row in valuation_rows:
            key = (row.code, row.indicator, row.trade_date)
            previous = valuations.get(key)
            if previous is None or previous.trade_date < row.trade_date:
                valuations[key] = row
        financial_rows = db.scalars(
            select(StockFinancialIndicator).where(
                StockFinancialIndicator.code.in_(universe)
            )
        ).all()
        fallback_financial: defaultdict[
            tuple[str, date], list[StockFinancialIndicator]
        ] = defaultdict(list)
        for row in financial_rows:
            fallback_financial[(row.code, row.report_date)].append(row)

        for code in universe:
            pit = daily.get(code)
            if pit is not None:
                pit_day = _as_date(pit[0])
                decide(
                    dataset="daily_price",
                    code=code,
                    effective_date=pit_day,
                    field_name="close",
                    candidates=[("tushare", pit[1])],
                    threshold=0.002,
                )
                for offset, field in ((2, "pe_ttm"), (3, "pb")):
                    fallback = valuations.get((code, field, pit_day))
                    candidates = [("tushare", pit[offset])]
                    if fallback is not None:
                        candidates.append(
                            (
                                fallback.source,
                                float(fallback.value)
                                if fallback.value is not None
                                else None,
                            )
                        )
                    decide(
                        dataset="valuation",
                        code=code,
                        effective_date=pit_day,
                        field_name=field,
                        candidates=_unique_source_candidates(candidates),
                        threshold=0.05 if field == "pe_ttm" else 0.02,
                        optional_if_all_missing=(
                            field == "pe_ttm" and pit[3] is not None
                        ),
                    )
            pit_financial = financial.get(code)
            if pit_financial is not None:
                report_day = _as_date(pit_financial[0])
                candidates = [("tushare", pit_financial[1])]
                candidates.extend(
                    (
                        row.source,
                        float(row.roe) if row.roe is not None else None,
                    )
                    for row in fallback_financial.get((code, report_day), [])
                )
                decide(
                    dataset="financial",
                    code=code,
                    effective_date=report_day,
                    field_name="roe",
                    candidates=_unique_source_candidates(candidates),
                    threshold=0.02,
                )

        industry_rows = db.scalars(
            select(StockIndustry).where(StockIndustry.code.in_(universe))
        ).all()
        industries: defaultdict[str, list[StockIndustry]] = defaultdict(list)
        for row in industry_rows:
            industries[row.code].append(row)
        taxonomy_crosswalk: defaultdict[tuple[str, str], Counter[str]] = defaultdict(
            Counter
        )
        for rows in industries.values():
            primary = next(
                (
                    row
                    for row in rows
                    if _source_root(row.source) == "stocktoday_sw2021"
                ),
                None,
            )
            if primary is None:
                continue
            for row in rows:
                root = _source_root(row.source)
                if root != "stocktoday_sw2021":
                    taxonomy_crosswalk[(root, row.industry_name)][
                        primary.industry_name
                    ] += 1
        for code, rows in industries.items():
            primary = next(
                (
                    row
                    for row in rows
                    if _source_root(row.source) == "stocktoday_sw2021"
                ),
                None,
            )
            if primary is None:
                continue
            candidates: list[tuple[str, object]] = [
                ("stocktoday_sw2021", primary.industry_name)
            ]
            for row in rows:
                root = _source_root(row.source)
                if root == "stocktoday_sw2021":
                    continue
                counts = taxonomy_crosswalk[(root, row.industry_name)].copy()
                counts[primary.industry_name] -= 1
                if counts[primary.industry_name] <= 0:
                    del counts[primary.industry_name]
                support = sum(counts.values())
                if support < 2:
                    continue
                mapped, mapped_count = counts.most_common(1)[0]
                confidence = mapped_count / support
                if confidence < 0.80:
                    continue
                candidates.append(
                    (
                        f"{root}:taxonomy_crosswalk:"
                        f"{row.industry_name}:n={support}:p={confidence:.3f}",
                        mapped,
                    )
                )
            decide(
                dataset="industry_classification",
                code=code,
                effective_date=as_of,
                field_name="industry_name",
                candidates=candidates,
                categorical_mismatch_status="taxonomy_divergence",
            )

        corporate = db.scalars(
            select(QuantDataRecord).where(
                QuantDataRecord.dataset.in_(("corporate_action", "dividend")),
                QuantDataRecord.effective_date <= as_of,
                QuantDataRecord.effective_date >= as_of - timedelta(days=365),
            )
        ).all()
        events: defaultdict[tuple[str, date, str], list[tuple[str, object]]] = (
            defaultdict(list)
        )
        for row in corporate:
            kind = str(row.payload.get("kind") or "dividend")
            events[(row.code, row.effective_date, kind)].append(
                (row.source, row.payload)
            )
        for (code, event_date, kind), candidates in events.items():
            comparable = _unique_source_candidates(candidates)
            if comparable:
                decide(
                    dataset="corporate_action",
                    code=code,
                    effective_date=event_date,
                    field_name=kind,
                    candidates=[
                        (
                            source,
                            str(sorted(dict(value).items())),
                        )
                        for source, value in comparable
                    ],
                )
        db.commit()
        return {
            "as_of": as_of.isoformat(),
            "universe": len(universe),
            "decisions": decisions,
            "statuses": dict(statuses),
        }
    finally:
        warehouse.close()
