"""决策价到成交/未成交的完整实现损失桥接与多维汇总。"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable


def order_tca(
    *,
    side: str,
    shares: float,
    decision_price: float,
    arrival_price: float,
    market_vwap: float,
    close_price: float,
    fill_price: float | None,
    fee: float,
    unfilled_shares: float = 0.0,
) -> dict[str, float]:
    sign = 1.0 if side == "buy" else -1.0
    filled = max(shares - unfilled_shares, 0.0)
    delay = sign * (arrival_price - decision_price) * filled
    market_move = sign * (market_vwap - arrival_price) * filled
    spread_impact = (
        sign * (float(fill_price) - market_vwap) * filled
        if fill_price is not None
        else 0.0
    )
    opportunity = sign * (close_price - decision_price) * unfilled_shares
    total = delay + market_move + spread_impact + fee + opportunity
    direct = (
        sign * (float(fill_price) - decision_price) * filled + fee + opportunity
        if fill_price is not None
        else fee + opportunity
    )
    return {
        "delay_cost": delay,
        "market_movement": market_move,
        "spread_and_impact": spread_impact,
        "opportunity_cost": opportunity,
        "fees": fee,
        "implementation_shortfall_amount": total,
        "direct_shortfall_amount": direct,
        "bridge_error": total - direct,
    }


def aggregate_tca(
    rows: Iterable[dict[str, object]],
) -> dict[str, object]:
    dimensions = ("code", "size_bucket", "session", "execution_algorithm")
    components = (
        "delay_cost",
        "market_movement",
        "spread_and_impact",
        "opportunity_cost",
        "fees",
        "implementation_shortfall_amount",
    )
    grouped: dict[str, dict[str, dict[str, float]]] = {
        dimension: defaultdict(lambda: defaultdict(float))
        for dimension in dimensions
    }
    total = defaultdict(float)
    for row in rows:
        for component in components:
            value = float(row.get(component) or 0.0)
            total[component] += value
            for dimension in dimensions:
                grouped[dimension][str(row.get(dimension, "unknown"))][
                    component
                ] += value
    return {
        "portfolio": dict(total),
        "by_dimension": {
            dimension: {
                key: dict(values) for key, values in groups.items()
            }
            for dimension, groups in grouped.items()
        },
    }
