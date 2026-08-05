"""未成交调仓订单的统一时效、衰减、偏离和机会成本规则。"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class OrderLifecyclePolicy:
    """回测、前向模拟共用的订单生命周期政策。"""

    version: str = "ORDER_LIFECYCLE_CN_STOCK_V1"
    ttl_trading_days: int = 5
    max_attempts: int = 5
    signal_decay_per_attempt: float = 0.10
    minimum_signal_strength: float = 0.50
    max_price_deviation: float = 0.15

    def __post_init__(self) -> None:
        if self.ttl_trading_days < 1 or self.max_attempts < 1:
            raise ValueError("订单 TTL 和最大重试次数必须至少为 1")
        if not 0 <= self.signal_decay_per_attempt < 1:
            raise ValueError("信号逐次衰减率必须位于 [0, 1)")
        if not 0 <= self.minimum_signal_strength <= 1:
            raise ValueError("最小信号强度必须位于 [0, 1]")
        if self.max_price_deviation <= 0:
            raise ValueError("最大价格偏离必须大于 0")


DEFAULT_POLICY = OrderLifecyclePolicy()
TERMINAL_STATUSES = {
    "filled",
    "expired",
    "cancelled",
    "superseded",
    "price_deviation_cancelled",
}


def signal_strength(
    attempts_before_execution: int, policy: OrderLifecyclePolicy
) -> float:
    """第 N 次重试时剩余信号强度；首次尝试为 100%。"""
    return max(
        policy.minimum_signal_strength,
        1.0 - max(attempts_before_execution, 0) * policy.signal_decay_per_attempt,
    )


def decayed_target_weight(
    *,
    current_weight: float,
    original_target_weight: float,
    attempts_before_execution: int,
    policy: OrderLifecyclePolicy,
) -> tuple[float, float]:
    """把目标向当前持仓收缩，避免陈旧信号继续按原强度成交。"""
    strength = signal_strength(attempts_before_execution, policy)
    return (
        current_weight + (original_target_weight - current_weight) * strength,
        strength,
    )


def price_deviation(
    decision_price: float | None, current_price: float | None
) -> float | None:
    if (
        decision_price is None
        or current_price is None
        or decision_price <= 0
        or current_price <= 0
    ):
        return None
    return current_price / decision_price - 1.0


def should_cancel_for_price_deviation(
    decision_price: float | None,
    current_price: float | None,
    policy: OrderLifecyclePolicy,
) -> tuple[bool, float | None]:
    deviation = price_deviation(decision_price, current_price)
    return (
        deviation is not None and abs(deviation) > policy.max_price_deviation,
        deviation,
    )


def is_expired(
    *,
    attempts: int,
    trading_days_elapsed: int,
    policy: OrderLifecyclePolicy,
) -> bool:
    return (
        attempts >= policy.max_attempts
        or trading_days_elapsed > policy.ttl_trading_days
    )


def opportunity_cost(
    *,
    side: str,
    unfilled_shares: float,
    decision_price: float | None,
    current_price: float | None,
) -> dict[str, float | None]:
    """计算未成交腿相对决策价的有符号和不利机会成本。

    买入后上涨、卖出后下跌为正的不利成本；相反方向保留为负的有符号成本，
    便于审计而不把有利未成交伪装成损失。
    """
    deviation = price_deviation(decision_price, current_price)
    if deviation is None or unfilled_shares <= 0:
        return {
            "signed_opportunity_cost": None,
            "adverse_opportunity_cost": None,
            "price_deviation": deviation,
        }
    direction = 1.0 if side == "buy" else -1.0
    signed = (
        direction
        * (float(current_price) - float(decision_price))
        * float(unfilled_shares)
    )
    return {
        "signed_opportunity_cost": signed,
        "adverse_opportunity_cost": max(signed, 0.0),
        "price_deviation": deviation,
    }
