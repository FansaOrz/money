"""东方财富银行主要指标到内部金融专用因子契约的规范化。"""

from __future__ import annotations

import math
from datetime import UTC, date, datetime
from typing import Any, Mapping


def _number(value: object) -> float | None:
    try:
        result = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _day(value: object) -> date | None:
    if value is None:
        return None
    try:
        return datetime.fromisoformat(str(value).split(" ")[0]).date()
    except ValueError:
        return None


def _available_at(value: object) -> datetime | None:
    day = _day(value)
    return datetime.combine(day, datetime.min.time(), tzinfo=UTC) if day else None


def _percent(value: object) -> float | None:
    number = _number(value)
    return number / 100.0 if number is not None else None


def normalize_bank_indicator(
    row: Mapping[str, Any],
) -> tuple[date, datetime, dict[str, float]] | None:
    """规范一行东财银行指标；缺失字段保留缺失，不制造监管数据。

    东财的净息差、不良率、拨备覆盖率和资本充足率使用百分数口径；
    ``LTDRR`` 是贷款/存款原始比值（约 1.0），无需再次除以 100。
    """
    report_period = _day(row.get("REPORT_DATE"))
    available = _available_at(row.get("NOTICE_DATE"))
    if report_period is None or available is None:
        return None
    loan_deposit = _number(row.get("LTDRR"))
    if loan_deposit is None:
        loans = _number(row.get("GROSSLOANS"))
        deposits = _number(row.get("TOTALDEPOSITS"))
        if loans is not None and deposits is not None and deposits > 0:
            loan_deposit = loans / deposits
    candidates = {
        "bank_net_interest_margin": _percent(row.get("NET_INTEREST_MARGIN")),
        "bank_npl_ratio": _percent(row.get("NONPERLOAN")),
        "bank_provision_coverage_ratio": _percent(row.get("BLDKBBL")),
        "bank_capital_adequacy_ratio": _percent(row.get("NEWCAPITALADER")),
        "bank_loan_deposit_ratio": loan_deposit,
    }
    metrics = {key: value for key, value in candidates.items() if value is not None}
    if not metrics:
        return None
    return report_period, available, metrics
