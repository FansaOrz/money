"""对价格、财务、估值、行业和公司行为执行跨源批量复核。"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta
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
from app.services.stock_repository import load_repository


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
    return {
        str(row[0]).split(".")[0]: tuple(row[1:])
        for row in rows
    }


def run_cross_source_reconciliation(
    db: Session,
    *,
    as_of: date,
    warehouse_path: Path | None = None,
) -> dict[str, object]:
    universe = sorted(
        set(
            db.scalars(
                select(IndexConstituent.stock_code).distinct()
            ).all()
        )
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
            warehouse, "daily_basic", ("close", "pe_ttm", "pb"), as_of
        )
        financial = _pit_latest(
            warehouse, "fina_indicator", ("roe",), as_of
        )
        repository = load_repository(db)
        latest_raw = {}
        if repository is not None:
            for bar in repository.daily_bars(
                universe,
                start=as_of - timedelta(days=10),
                end=as_of,
            ):
                prior = latest_raw.get(bar.code)
                if prior is None or prior.trade_date < bar.trade_date:
                    latest_raw[bar.code] = bar
        valuation_rows = db.scalars(
            select(StockValuation).where(
                StockValuation.code.in_(universe),
                StockValuation.trade_date <= as_of,
                StockValuation.trade_date >= as_of - timedelta(days=10),
                StockValuation.indicator.in_(("pe_ttm", "pb")),
            )
        ).all()
        valuations: dict[tuple[str, str], StockValuation] = {}
        for row in valuation_rows:
            key = (row.code, row.indicator)
            previous = valuations.get(key)
            if previous is None or previous.trade_date < row.trade_date:
                valuations[key] = row
        financial_rows = db.scalars(
            select(StockFinancialIndicator).where(
                StockFinancialIndicator.code.in_(universe)
            )
        ).all()
        fallback_financial: dict[str, StockFinancialIndicator] = {}
        for row in financial_rows:
            previous = fallback_financial.get(row.code)
            if previous is None or previous.report_date < row.report_date:
                fallback_financial[row.code] = row

        for code in universe:
            pit = daily.get(code)
            raw = latest_raw.get(code)
            if pit is not None and raw is not None:
                decide(
                    dataset="daily_price",
                    code=code,
                    effective_date=as_of,
                    field_name="close",
                    candidates=[
                        ("tushare", pit[0]),
                        ("sina", raw.close),
                    ],
                    threshold=0.002,
                )
            if pit is not None:
                for offset, field in ((1, "pe_ttm"), (2, "pb")):
                    fallback = valuations.get((code, field))
                    if fallback is not None:
                        decide(
                            dataset="valuation",
                            code=code,
                            effective_date=as_of,
                            field_name=field,
                            candidates=[
                                ("tushare", pit[offset]),
                                (
                                    "baidu",
                                    float(fallback.value)
                                    if fallback.value is not None
                                    else None,
                                ),
                            ],
                            threshold=0.05 if field == "pe_ttm" else 0.02,
                        )
            pit_financial = financial.get(code)
            fallback = fallback_financial.get(code)
            if pit_financial is not None and fallback is not None:
                decide(
                    dataset="financial",
                    code=code,
                    effective_date=fallback.report_date,
                    field_name="roe",
                    candidates=[
                        ("tushare", pit_financial[0]),
                        ("sina", float(fallback.roe) if fallback.roe else None),
                    ],
                    threshold=0.02,
                )

        industry_rows = db.scalars(
            select(StockIndustry).where(StockIndustry.code.in_(universe))
        ).all()
        industries: defaultdict[str, list[tuple[str, object]]] = defaultdict(list)
        for row in industry_rows:
            industries[row.code].append((row.source, row.industry_name))
        for code, candidates in industries.items():
            if len(candidates) >= 2:
                decide(
                    dataset="industry_classification",
                    code=code,
                    effective_date=as_of,
                    field_name="industry_name",
                    candidates=candidates,
                )

        corporate = db.scalars(
            select(QuantDataRecord).where(
                QuantDataRecord.dataset.in_(
                    ("corporate_action", "dividend")
                ),
                QuantDataRecord.effective_date <= as_of,
                QuantDataRecord.effective_date
                >= as_of - timedelta(days=365),
            )
        ).all()
        events: defaultdict[
            tuple[str, date, str], list[tuple[str, object]]
        ] = defaultdict(list)
        for row in corporate:
            kind = str(row.payload.get("kind") or "dividend")
            events[(row.code, row.effective_date, kind)].append(
                (row.source.split(":")[0], row.payload)
            )
        for (code, event_date, kind), candidates in events.items():
            if len(candidates) >= 2:
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
                        for source, value in candidates
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
