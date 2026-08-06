"""组合压力、尾部风险、离散交易和容量/清算闭环。"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from statistics import fmean

import numpy as np


def tail_risk(
    portfolio_returns: Sequence[float],
    *,
    confidence: float = 0.95,
) -> dict[str, float | None]:
    values = sorted(float(value) for value in portfolio_returns)
    if not values:
        return {"var": None, "cvar": None, "cdar": None}
    tail_count = max(1, math.ceil((1.0 - confidence) * len(values)))
    var = values[tail_count - 1]
    cvar = fmean(values[:tail_count])
    wealth = 1.0
    peak = 1.0
    drawdowns: list[float] = []
    for value in portfolio_returns:
        wealth *= 1.0 + float(value)
        peak = max(peak, wealth)
        drawdowns.append(wealth / peak - 1.0)
    drawdown_count = max(1, math.ceil((1.0 - confidence) * len(drawdowns)))
    cdar = fmean(sorted(drawdowns)[:drawdown_count])
    return {"var": var, "cvar": cvar, "cdar": cdar}


def stress_test(
    *,
    codes: Sequence[str],
    weights: Sequence[float],
    position_values: Sequence[float],
    adv_amounts: Sequence[float],
    industry_by_code: Mapping[str, str],
    historical_crisis_returns: Mapping[str, Sequence[float]] | None = None,
    suspended_codes: set[str] | None = None,
    consecutive_limit_down_codes: set[str] | None = None,
    normal_participation: float = 0.10,
) -> dict[str, object]:
    """逐股票解释历史危机、跳空、波动/相关上升、流动性枯竭和连续跌停。"""
    vector = np.asarray(weights, dtype=float)
    scenarios: dict[str, np.ndarray] = {
        "market_gap_down_10pct": np.full(len(codes), -0.10),
        "volatility_double_left_tail": np.full(len(codes), -0.15),
        "correlation_to_one": np.full(len(codes), -0.12),
    }
    for name, values in (historical_crisis_returns or {}).items():
        if len(values) != len(codes):
            raise ValueError(f"压力情景 {name} 与证券维度不一致")
        scenarios[f"historical:{name}"] = np.asarray(values, dtype=float)
    industries = sorted(set(industry_by_code.values()))
    for industry in industries:
        scenarios[f"industry_shock:{industry}"] = np.asarray(
            [
                -0.20 if industry_by_code.get(code) == industry else -0.03
                for code in codes
            ]
        )
    unable = set(suspended_codes or set()) | set(consecutive_limit_down_codes or set())
    reports: list[dict[str, object]] = []
    for name, shocks in scenarios.items():
        contribution = vector * shocks
        reports.append(
            {
                "scenario": name,
                "portfolio_pnl_rate": float(contribution.sum()),
                "asset_pnl_contribution": dict(
                    zip(codes, contribution.tolist(), strict=True)
                ),
            }
        )
    liquidation: dict[str, dict[str, object]] = {}
    for code, value, adv in zip(codes, position_values, adv_amounts, strict=True):
        days_normal = abs(float(value)) / max(float(adv) * normal_participation, 1.0)
        days_stress = (
            None
            if code in unable
            else abs(float(value)) / max(float(adv) * 0.30 * normal_participation, 1.0)
        )
        liquidation[code] = {
            "normal_days": days_normal,
            "stress_days": days_stress,
            "tradable": code not in unable,
            "reason": (
                "suspended_or_consecutive_limit_down" if code in unable else None
            ),
        }
    worst = min(reports, key=lambda row: float(row["portfolio_pnl_rate"]))
    return {
        "scenarios": reports,
        "worst_scenario": worst["scenario"],
        "worst_portfolio_pnl_rate": worst["portfolio_pnl_rate"],
        "liquidation": liquidation,
        "unable_to_trade": sorted(unable),
        "cash_requirement": sum(
            max(-float(row["portfolio_pnl_rate"]), 0.0) for row in reports
        ),
        "passed": (float(worst["portfolio_pnl_rate"]) >= -0.30 and not unable),
        "model_version": "PORTFOLIO_STRESS_V1",
    }


def discretize_portfolio(
    *,
    target_weights: Mapping[str, float],
    prices: Mapping[str, float],
    portfolio_value: float,
    lot_sizes: Mapping[str, int] | None = None,
    covariance: np.ndarray | None = None,
    benchmark_weights: Mapping[str, float] | None = None,
    max_stock_weight: float = 0.05,
    max_tracking_error: float = 0.12,
    minimum_holdings: int = 0,
) -> dict[str, object]:
    """按证券交易单位向下取整并重新计算现金、权重和真实 TE。"""
    codes = list(target_weights)
    shares: dict[str, int] = {}
    for code in codes:
        price = float(prices[code])
        lot = int((lot_sizes or {}).get(code, 100))
        if lot <= 0 or price <= 0:
            raise ValueError("交易单位与价格必须为正")
        shares[code] = int(
            math.floor(portfolio_value * float(target_weights[code]) / price / lot)
            * lot
        )
    actual_values = {code: shares[code] * float(prices[code]) for code in codes}
    invested = sum(actual_values.values())
    actual_weights = {
        code: value / portfolio_value for code, value in actual_values.items()
    }
    violations: list[dict[str, object]] = []
    for code, weight in actual_weights.items():
        if weight > max_stock_weight + 1e-10:
            violations.append(
                {"constraint": "max_stock_weight", "code": code, "actual": weight}
            )
    executable_holdings = sum(shares[code] > 0 for code in codes)
    if executable_holdings < max(int(minimum_holdings), 0):
        violations.append(
            {
                "constraint": "minimum_holdings",
                "actual": executable_holdings,
                "limit": max(int(minimum_holdings), 0),
            }
        )
    tracking_error = None
    if covariance is not None and benchmark_weights is not None:
        active = np.asarray(
            [
                actual_weights.get(code, 0.0) - float(benchmark_weights.get(code, 0.0))
                for code in codes
            ]
        )
        tracking_error = math.sqrt(
            max(float(active @ covariance @ active), 0.0) * 252.0
        )
        if tracking_error > max_tracking_error:
            violations.append(
                {
                    "constraint": "max_tracking_error",
                    "actual": tracking_error,
                    "limit": max_tracking_error,
                }
            )
    return {
        "shares": shares,
        "actual_weights": actual_weights,
        "cash": portfolio_value - invested,
        "cash_weight": 1.0 - invested / portfolio_value,
        "executable_holdings": executable_holdings,
        "weight_deviation": {
            code: actual_weights[code] - float(target_weights[code]) for code in codes
        },
        "tracking_error": tracking_error,
        "violations": violations,
        "passed": not violations,
        "repair_method": "floor_to_board_lot_then_recalculate_all_hard_constraints",
    }


def capacity_curve(
    *,
    codes: Sequence[str],
    target_weights: Sequence[float],
    adv_amounts: Sequence[float],
    capital_levels: Sequence[float],
    gross_expected_return: float,
    base_cost_rate: float = 0.001,
    impact_coefficient: float = 0.002,
    approved_max_liquidation_days: float = 5.0,
) -> dict[str, object]:
    """资金规模→净收益、冲击、集中度和正常/压力清算天数曲线。"""
    rows: list[dict[str, object]] = []
    approved_capital = 0.0
    weights = np.asarray(target_weights, dtype=float)
    if len(codes) == 0:
        return {
            "curve": [],
            "maximum_approved_capital": 0.0,
            "binding_policy": {
                "max_stress_liquidation_days": approved_max_liquidation_days,
                "net_expected_return_must_be_positive": True,
            },
        }
    for capital in sorted(float(value) for value in capital_levels):
        values = weights * capital
        participation = values / np.maximum(np.asarray(adv_amounts), 1.0)
        impact = float(
            np.sum(
                weights * impact_coefficient * np.sqrt(np.maximum(participation, 0.0))
            )
        )
        normal_days = float(np.max(participation / 0.10))
        stress_days = float(np.max(participation / 0.03))
        net_return = gross_expected_return - base_cost_rate - impact
        approved = net_return > 0 and stress_days <= approved_max_liquidation_days
        if approved:
            approved_capital = capital
        rows.append(
            {
                "capital": capital,
                "net_expected_return": net_return,
                "market_impact": impact,
                "maximum_weight": float(weights.max()),
                "normal_liquidation_days": normal_days,
                "stress_liquidation_days": stress_days,
                "approved": approved,
            }
        )
    return {
        "curve": rows,
        "maximum_approved_capital": approved_capital,
        "binding_policy": {
            "max_stress_liquidation_days": approved_max_liquidation_days,
            "net_expected_return_must_be_positive": True,
        },
    }
