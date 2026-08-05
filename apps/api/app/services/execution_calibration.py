"""用前向/券商 TCA 观测校准开盘 ADV 平方根冲击模型。"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime
import hashlib
import json
import math
from statistics import fmean

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import QuantDataRecord, StockPaperTrade
from app.services import trading_rules


@dataclass(frozen=True)
class ExecutionObservation:
    code: str
    trade_date: date
    side: str
    implementation_shortfall: float
    participation_rate: float
    recent_volatility: float
    liquidity_adv: float
    execution_session: str = "open"


def _bucket(value: float, cuts: tuple[float, float], labels: tuple[str, str, str]) -> str:
    if value < cuts[0]:
        return labels[0]
    if value < cuts[1]:
        return labels[1]
    return labels[2]


def calibrate_observations(
    observations: list[ExecutionObservation],
    *,
    baseline_slippage: float = 0.001,
) -> dict[str, object]:
    """拟合 shortfall = 固定滑点 + impact*sqrt(participation)+vol_coef*vol。"""
    if len(observations) < 3:
        return {
            "status": "insufficient",
            "sample_size": len(observations),
            "minimum_required": 3,
        }
    xs = [
        (
            math.sqrt(max(item.participation_rate, 0.0)),
            max(item.recent_volatility, 0.0),
        )
        for item in observations
    ]
    ys = [
        item.implementation_shortfall - baseline_slippage
        for item in observations
    ]
    xx = sum(x * x for x, _v in xs)
    vv = sum(v * v for _x, v in xs)
    xv = sum(x * v for x, v in xs)
    xy = sum(x * y for (x, _v), y in zip(xs, ys, strict=True))
    vy = sum(v * y for (_x, v), y in zip(xs, ys, strict=True))
    determinant = xx * vv - xv * xv
    if abs(determinant) <= 1e-15:
        impact = xy / xx if xx > 0 else 0.0
        volatility_coefficient = 0.0
    else:
        impact = (xy * vv - vy * xv) / determinant
        volatility_coefficient = (vy * xx - xy * xv) / determinant
    impact = max(impact, 0.0)
    volatility_coefficient = max(volatility_coefficient, 0.0)
    predictions = [
        baseline_slippage + impact * x + volatility_coefficient * vol
        for x, vol in xs
    ]
    errors = [
        prediction - item.implementation_shortfall
        for prediction, item in zip(predictions, observations, strict=True)
    ]
    groups: dict[str, list[float]] = {}
    for item in observations:
        board = trading_rules.quantity_rule(
            item.code, item.trade_date
        ).board
        key = "|".join(
            (
                board,
                item.side,
                item.execution_session,
                _bucket(
                    item.liquidity_adv,
                    (1_000_000, 10_000_000),
                    ("low_adv", "mid_adv", "high_adv"),
                ),
                _bucket(
                    item.recent_volatility,
                    (0.015, 0.035),
                    ("low_vol", "mid_vol", "high_vol"),
                ),
                _bucket(
                    item.participation_rate,
                    (0.01, 0.05),
                    ("small", "medium", "large"),
                ),
            )
        )
        groups.setdefault(key, []).append(item.implementation_shortfall)
    return {
        "status": "calibrated",
        "model_version": "OPEN_ADV_SQRT_CALIBRATED_V1",
        "sample_size": len(observations),
        "baseline_slippage": baseline_slippage,
        "market_impact_coefficient": impact,
        "volatility_slippage_coefficient": volatility_coefficient,
        "mean_observed_shortfall": fmean(
            item.implementation_shortfall for item in observations
        ),
        "calibration_bias": fmean(errors),
        "calibration_mae": fmean(abs(value) for value in errors),
        "groups": {
            key: {"sample_size": len(values), "mean_shortfall": fmean(values)}
            for key, values in sorted(groups.items())
        },
    }


def calibrate_and_persist(db: Session) -> dict[str, object]:
    rows = list(
        db.scalars(
            select(StockPaperTrade).where(
                StockPaperTrade.implementation_shortfall.is_not(None),
                StockPaperTrade.participation_rate.is_not(None),
                StockPaperTrade.recent_volatility.is_not(None),
                StockPaperTrade.liquidity_adv.is_not(None),
            )
        ).all()
    )
    observations = [
        ExecutionObservation(
            code=row.stock_code,
            trade_date=row.trade_date,
            side=row.side,
            implementation_shortfall=float(row.implementation_shortfall),
            participation_rate=float(row.participation_rate),
            recent_volatility=float(row.recent_volatility),
            liquidity_adv=float(row.liquidity_adv),
            execution_session=row.execution_session,
        )
        for row in rows
    ]
    result = calibrate_observations(observations)
    canonical = json.dumps(
        {
            "observations": [asdict(item) for item in observations],
            "result": result,
        },
        default=str,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    source_hash = hashlib.sha256(canonical.encode()).hexdigest()
    now = datetime.now(UTC)
    existing = db.scalar(
        select(QuantDataRecord).where(
            QuantDataRecord.dataset == "execution_calibration",
            QuantDataRecord.code == "GLOBAL",
            QuantDataRecord.effective_date == now.date(),
            QuantDataRecord.source_hash == source_hash,
        )
    )
    if existing is None:
        db.add(
            QuantDataRecord(
                dataset="execution_calibration",
                code="GLOBAL",
                effective_date=now.date(),
                available_at=now,
                source=f"internal:tca:{source_hash[:12]}",
                source_file="database:stock_paper_trades",
                source_hash=source_hash,
                payload=result,
                imported_at=now,
            )
        )
        db.commit()
    return {**result, "source_hash": source_hash}
