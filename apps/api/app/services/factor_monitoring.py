"""因子衰减、拥挤、容量和稳健 CUSUM 结构突变监控。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from statistics import fmean, median

POLICY_VERSION = "FACTOR_MONITOR_V1"


@dataclass(frozen=True)
class FactorPeriodMetric:
    as_of: date
    rank_ic: float | None
    top_minus_bottom_return: float | None
    turnover: float | None
    capacity_ratio: float | None
    maximum_peer_correlation: float | None
    exposure: float | None


@dataclass(frozen=True)
class FactorMonitorDecision:
    factor: str
    as_of: date
    action: str
    weight_multiplier: float
    reasons: tuple[str, ...]
    metrics: dict[str, object]


def _robust_cusum(values: list[float], threshold: float = 5.0) -> tuple[bool, float]:
    if len(values) < 12:
        return False, 0.0
    baseline = values[:-3]
    center = median(baseline)
    deviations = [abs(value - center) for value in baseline]
    mad = median(deviations) or 1e-6
    scale = 1.4826 * mad
    positive = negative = maximum = 0.0
    for value in values:
        standardized = (value - center) / scale
        positive = max(0.0, positive + standardized - 0.5)
        negative = min(0.0, negative + standardized + 0.5)
        maximum = max(maximum, positive, abs(negative))
    return maximum >= threshold, maximum


def monitor_factor(
    factor: str,
    history: list[FactorPeriodMetric],
    *,
    rolling_periods: int = 12,
) -> FactorMonitorDecision:
    if not history:
        raise ValueError("因子监控历史不能为空")
    ordered = sorted(history, key=lambda item: item.as_of)
    recent = ordered[-rolling_periods:]
    ics = [item.rank_ic for item in recent if item.rank_ic is not None]
    spreads = [
        item.top_minus_bottom_return
        for item in recent
        if item.top_minus_bottom_return is not None
    ]
    reasons: list[str] = []
    action = "keep"
    multiplier = 1.0
    negative_streak = 0
    for item in reversed(recent):
        if item.rank_ic is not None and item.rank_ic < 0:
            negative_streak += 1
        else:
            break
    mean_ic = fmean(ics) if ics else None
    mean_spread = fmean(spreads) if spreads else None
    if negative_streak >= 6 or (
        len(ics) >= rolling_periods and mean_ic is not None and mean_ic < -0.01
    ):
        action = "pause"
        multiplier = 0.0
        reasons.append("Rank IC 持续失效达到暂停阈值")
    elif len(ics) >= 6 and mean_ic is not None and mean_ic <= 0:
        action = "downweight"
        multiplier = 0.5
        reasons.append("滚动 Rank IC 非正，权重减半")
    latest = recent[-1]
    if latest.capacity_ratio is not None and latest.capacity_ratio > 0.80:
        action = "pause" if latest.capacity_ratio > 1.0 else "downweight"
        multiplier = 0.0 if latest.capacity_ratio > 1.0 else min(multiplier, 0.5)
        reasons.append("因子组合容量使用率过高")
    if (
        latest.maximum_peer_correlation is not None
        and abs(latest.maximum_peer_correlation) > 0.90
    ):
        if action == "keep":
            action = "downweight"
            multiplier = 0.5
        reasons.append("与同类因子相关超过拥挤阈值")
    ic_change, ic_cusum = _robust_cusum(
        [item.rank_ic for item in ordered if item.rank_ic is not None]
    )
    exposure_change, exposure_cusum = _robust_cusum(
        [item.exposure for item in ordered if item.exposure is not None]
    )
    if ic_change or exposure_change:
        if action != "pause":
            action = "retrain_review"
            multiplier = min(multiplier, 0.5)
        reasons.append("稳健 CUSUM 检出结构突变，进入重训/复核")
    metrics = {
        "rolling_periods": rolling_periods,
        "rank_ic_mean": mean_ic,
        "top_minus_bottom_mean": mean_spread,
        "negative_ic_streak": negative_streak,
        "latest_turnover": latest.turnover,
        "latest_capacity_ratio": latest.capacity_ratio,
        "latest_maximum_peer_correlation": latest.maximum_peer_correlation,
        "latest_exposure": latest.exposure,
        "ic_cusum": ic_cusum,
        "exposure_cusum": exposure_cusum,
    }
    return FactorMonitorDecision(
        factor=factor,
        as_of=latest.as_of,
        action=action,
        weight_multiplier=multiplier,
        reasons=tuple(reasons),
        metrics=metrics,
    )


def persist_monitor_decision(
    db: object,
    decision: FactorMonitorDecision,
    *,
    strategy_version_id: int | None,
) -> object:
    from sqlalchemy import delete

    from app.models import FactorMonitorSnapshot

    db.execute(  # type: ignore[attr-defined]
        delete(FactorMonitorSnapshot).where(
            FactorMonitorSnapshot.strategy_version_id == strategy_version_id,
            FactorMonitorSnapshot.as_of == decision.as_of,
            FactorMonitorSnapshot.factor_name == decision.factor,
        )
    )
    row = FactorMonitorSnapshot(
        strategy_version_id=strategy_version_id,
        as_of=decision.as_of,
        factor_name=decision.factor,
        action=decision.action,
        metrics={
            **decision.metrics,
            "weight_multiplier": decision.weight_multiplier,
        },
        reasons=list(decision.reasons),
        policy_version=POLICY_VERSION,
        created_at=datetime.now(UTC),
    )
    db.add(row)  # type: ignore[attr-defined]
    db.flush()  # type: ignore[attr-defined]
    return row
