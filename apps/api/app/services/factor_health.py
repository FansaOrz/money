"""每期因子分布健康检查；异常字段不得静默进入正式评分。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from math import isfinite
from statistics import median
from typing import Iterable

FACTOR_CONTRACTS: dict[str, dict[str, object]] = {
    "roe": {"unit": "ratio", "direction": 1, "sign_stable": False},
    "roa": {"unit": "ratio", "direction": 1, "sign_stable": False},
    "gross_margin": {"unit": "ratio", "direction": 1, "sign_stable": True},
    "debt_ratio": {"unit": "ratio", "direction": -1, "sign_stable": True},
    "cash_conversion_assets": {
        "unit": "ratio",
        "direction": 1,
        "sign_stable": False,
    },
    "ep": {"unit": "ratio", "direction": 1, "sign_stable": False},
    "bp": {"unit": "ratio", "direction": 1, "sign_stable": True},
    "sales_yield": {"unit": "ratio", "direction": 1, "sign_stable": True},
    "dividend_yield": {"unit": "ratio", "direction": 1, "sign_stable": True},
    "fcf_yield": {"unit": "ratio", "direction": 1, "sign_stable": False},
    "valuation_percentile": {
        "unit": "percentile",
        "direction": 1,
        "sign_stable": True,
    },
}


@dataclass(frozen=True)
class FactorHealth:
    factor: str
    total: int
    valid: int
    coverage: float
    unique_values: int
    minimum: float | None
    q01: float | None
    median: float | None
    q99: float | None
    maximum: float | None
    historical_median: float | None
    scale_ratio: float | None
    blocked: bool
    reasons: tuple[str, ...]


def _quantile(values: list[float], probability: float) -> float:
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = probability * (len(ordered) - 1)
    low = int(position)
    high = min(low + 1, len(ordered) - 1)
    fraction = position - low
    return ordered[low] * (1.0 - fraction) + ordered[high] * fraction


def inspect_factor(
    factor: str,
    values: list[float | None],
    *,
    minimum_coverage: float = 0.20,
    minimum_unique_values: int = 2,
    minimum_cross_section: int = 20,
    historical_median: float | None = None,
    check_historical_sign: bool = True,
    maximum_scale_ratio: float = 100.0,
) -> FactorHealth:
    valid = [float(value) for value in values if value is not None and isfinite(value)]
    coverage = len(valid) / len(values) if values else 0.0
    unique = len(set(valid))
    reasons: list[str] = []
    # 小股票池/单元测试无法据横截面分布判定字段异常，保留原有降级逻辑；
    # 正式沪深300+500股票池远高于此门槛。
    sufficiently_large = len(values) >= minimum_cross_section
    if sufficiently_large and coverage < minimum_coverage:
        reasons.append(
            f"覆盖率{coverage:.1%}低于门槛{minimum_coverage:.1%}"
        )
    if sufficiently_large and valid and unique < minimum_unique_values:
        reasons.append(f"唯一值数{unique}低于门槛{minimum_unique_values}")
    if valid:
        q01 = _quantile(valid, 0.01)
        q99 = _quantile(valid, 0.99)
        center = median(valid)
        scale = max(abs(center), 1e-12)
        if sufficiently_large and len(valid) >= 10 and (q99 - q01) / scale < 1e-8:
            reasons.append("近常数分布")
    else:
        q01 = q99 = center = None
    scale_ratio = None
    if (
        center is not None
        and historical_median is not None
        and abs(center) > 1e-12
        and abs(historical_median) > 1e-12
    ):
        scale_ratio = abs(center / historical_median)
        if (
            scale_ratio > maximum_scale_ratio
            or scale_ratio < 1.0 / maximum_scale_ratio
        ):
            reasons.append(
                f"中位数量级相对历史变化{scale_ratio:.6g}倍，疑似单位突变"
            )
        if check_historical_sign and center * historical_median < 0:
            reasons.append("中位数符号相对历史整体反转")
    return FactorHealth(
        factor=factor,
        total=len(values),
        valid=len(valid),
        coverage=coverage,
        unique_values=unique,
        minimum=min(valid) if valid else None,
        q01=q01,
        median=center,
        q99=q99,
        maximum=max(valid) if valid else None,
        historical_median=historical_median,
        scale_ratio=scale_ratio,
        blocked=bool(reasons),
        reasons=tuple(reasons),
    )


def persist_factor_health_reports(
    db: object,
    results: Iterable[object],
    *,
    signal_date: date,
    strategy_version_id: int | None,
    direction_map: dict[str, int],
) -> list[FactorHealth]:
    """计算、持久化全因子健康报告并返回本期阻断证据。"""
    from sqlalchemy import delete, select

    from app.models import FactorHealthReport

    rows = list(results)
    previous_rows = db.scalars(  # type: ignore[attr-defined]
        select(FactorHealthReport)
        .where(
            FactorHealthReport.strategy_version_id == strategy_version_id,
            FactorHealthReport.signal_date < signal_date,
        )
        .order_by(
            FactorHealthReport.factor_name,
            FactorHealthReport.signal_date.desc(),
        )
    ).all()
    previous_by_factor: dict[str, object] = {}
    for row in previous_rows:
        previous_by_factor.setdefault(row.factor_name, row)
    reports: list[FactorHealth] = []
    for factor, direction in direction_map.items():
        previous = previous_by_factor.get(factor)
        previous_statistics = (
            dict(previous.statistics or {}) if previous is not None else {}
        )
        historical = previous_statistics.get("median")
        try:
            historical_median = (
                float(historical) if historical is not None else None
            )
        except (TypeError, ValueError):
            historical_median = None
        contract = FACTOR_CONTRACTS.get(
            factor,
            {"unit": "dimensionless", "direction": direction, "sign_stable": False},
        )
        report = inspect_factor(
            factor,
            [dict(getattr(item, "raw", {}) or {}).get(factor) for item in rows],
            historical_median=historical_median,
            check_historical_sign=bool(contract.get("sign_stable")),
        )
        reports.append(report)
        db.execute(  # type: ignore[attr-defined]
            delete(FactorHealthReport).where(
                FactorHealthReport.strategy_version_id == strategy_version_id,
                FactorHealthReport.signal_date == signal_date,
                FactorHealthReport.factor_name == factor,
            )
        )
        db.add(  # type: ignore[attr-defined]
            FactorHealthReport(
                strategy_version_id=strategy_version_id,
                signal_date=signal_date,
                factor_name=factor,
                status="blocked" if report.blocked else "passed",
                unit=str(contract.get("unit") or "dimensionless"),
                direction=int(contract.get("direction") or direction),
                statistics={
                    "total": report.total,
                    "valid": report.valid,
                    "coverage": report.coverage,
                    "unique_values": report.unique_values,
                    "minimum": report.minimum,
                    "q01": report.q01,
                    "median": report.median,
                    "q99": report.q99,
                    "maximum": report.maximum,
                    "historical_median": report.historical_median,
                    "scale_ratio": report.scale_ratio,
                },
                reasons=list(report.reasons),
                created_at=datetime.now(UTC),
            )
        )
    db.flush()  # type: ignore[attr-defined]
    return reports
