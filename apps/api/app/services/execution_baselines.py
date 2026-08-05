"""TWAP、VWAP 与 Almgren–Chriss 执行基线。"""

from __future__ import annotations

import math
from collections.abc import Sequence


def twap(quantity: int, intervals: int) -> list[int]:
    base, remainder = divmod(quantity, intervals)
    return [base + (index < remainder) for index in range(intervals)]


def vwap(quantity: int, volume_profile: Sequence[float]) -> list[int]:
    total = sum(max(float(value), 0) for value in volume_profile)
    if total <= 0:
        return twap(quantity, len(volume_profile))
    raw = [quantity * max(float(value), 0) / total for value in volume_profile]
    allocations = [math.floor(value) for value in raw]
    for index in sorted(
        range(len(raw)), key=lambda item: raw[item] - allocations[item], reverse=True
    )[: quantity - sum(allocations)]:
        allocations[index] += 1
    return allocations


def almgren_chriss(
    quantity: int,
    intervals: int,
    *,
    risk_aversion: float,
    volatility: float,
    temporary_impact: float,
) -> list[int]:
    kappa = math.sqrt(
        max(risk_aversion, 0) * volatility**2 / max(temporary_impact, 1e-12)
    )
    if kappa < 1e-8:
        return twap(quantity, intervals)
    remaining = [
        quantity
        * math.sinh(kappa * (intervals - index))
        / math.sinh(kappa * intervals)
        for index in range(intervals + 1)
    ]
    raw = [max(remaining[index] - remaining[index + 1], 0) for index in range(intervals)]
    return vwap(quantity, raw)
