"""含风险、真实成本、换手与 Alpha 不确定性的统一凸组合优化。"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Sequence

import cvxpy as cp
import numpy as np


def estimate_trade_cost_rates(
    prices: Sequence[float],
    adv_amounts: Sequence[float],
    target_trade_values: Sequence[float],
    *,
    commission_rate: float = 0.00025,
    stamp_tax_rate: float = 0.0005,
    half_spread_rate: float = 0.0005,
    impact_coefficient: float = 0.002,
    minimum_commission: float = 5.0,
) -> dict[str, list[float]]:
    """把佣金、税、价差、sqrt(订单/ADV) 冲击和最低佣金转为逐股成本率。"""
    linear: list[float] = []
    impact: list[float] = []
    for price, adv, value in zip(prices, adv_amounts, target_trade_values, strict=True):
        amount = abs(float(value))
        minimum_rate = minimum_commission / amount if amount > 0 else 0.0
        linear.append(
            max(commission_rate, minimum_rate) + stamp_tax_rate / 2.0 + half_spread_rate
        )
        participation = amount / max(float(adv), float(price), 1.0)
        impact.append(impact_coefficient * math.sqrt(max(participation, 0.0)))
    return {"linear_cost_rates": linear, "impact_cost_rates": impact}


def diagnose_infeasibility(
    *,
    max_stock_weight: float,
    minimum_invested_weight: float,
    asset_count: int,
    max_turnover: float,
    current_weights: np.ndarray,
    adv_weight_limits: np.ndarray,
) -> dict[str, object]:
    conflicts: list[dict[str, object]] = []
    capacity = asset_count * max_stock_weight
    if capacity + 1e-12 < minimum_invested_weight:
        conflicts.append(
            {
                "constraint": "max_stock_weight_vs_minimum_invested",
                "minimum_relaxation": minimum_invested_weight - capacity,
            }
        )
    required = max(
        minimum_invested_weight - float(np.asarray(current_weights).sum()), 0.0
    )
    available = float(np.asarray(adv_weight_limits).sum())
    if available + 1e-12 < required:
        conflicts.append(
            {
                "constraint": "adv_trade_limit",
                "minimum_relaxation": required - available,
            }
        )
    return {
        "conflicts": conflicts,
        "hard_constraints": ["long_only", "cash_nonnegative", "legal_trade"],
        "soft_relaxation_order": [
            "style_active_exposure",
            "industry_active_weight",
            "max_turnover",
            "max_tracking_error",
        ],
        "max_turnover": max_turnover,
        "silent_risk_definition_change_forbidden": True,
    }


def optimize_portfolio(
    *,
    codes: Sequence[str],
    alpha: Sequence[float],
    covariance: np.ndarray,
    current_weights: Sequence[float],
    benchmark_weights: Sequence[float],
    alpha_standard_errors: Sequence[float] | None = None,
    linear_cost_rates: Sequence[float] | None = None,
    impact_cost_rates: Sequence[float] | None = None,
    industry_exposures: np.ndarray | None = None,
    benchmark_industry_exposures: Sequence[float] | None = None,
    style_exposures: np.ndarray | None = None,
    benchmark_style_exposures: Sequence[float] | None = None,
    adv_weight_limits: Sequence[float] | None = None,
    asset_weight_limits: Sequence[float] | None = None,
    scenario_returns: np.ndarray | None = None,
    max_cvar_loss: float | None = None,
    cvar_confidence: float = 0.95,
    max_stock_weight: float = 0.05,
    max_industry_active_weight: float = 0.03,
    max_style_active_exposure: float | Sequence[float] = 0.20,
    max_tracking_error: float = 0.12,
    max_annual_volatility: float = 0.20,
    max_turnover: float = 0.50,
    minimum_cash: float = 0.0,
    maximum_cash: float = 1.0,
    risk_aversion: float = 4.0,
    turnover_penalty: float = 0.001,
    alpha_uncertainty_penalty: float = 1.0,
    l2_regularization: float = 0.01,
) -> dict[str, object]:
    """求解 long-only QP/SOCP，并保存目标分解、余量、状态和输入哈希。"""
    count = len(codes)
    if any(
        len(values) != count for values in (alpha, current_weights, benchmark_weights)
    ):
        raise ValueError("优化输入证券维度不一致")
    covariance = np.asarray(covariance, dtype=float)
    if covariance.shape != (count, count):
        raise ValueError("协方差矩阵维度不一致")
    covariance = (covariance + covariance.T) / 2.0
    minimum_eigenvalue = float(np.linalg.eigvalsh(covariance).min())
    if minimum_eigenvalue < -1e-10:
        raise ValueError("协方差矩阵不是正半定")
    covariance += np.eye(count) * max(1e-12, -minimum_eigenvalue)
    raw_alpha = np.asarray(alpha, dtype=float)
    uncertainty = np.asarray(
        alpha_standard_errors if alpha_standard_errors is not None else np.zeros(count),
        dtype=float,
    )
    robust_alpha = 0.5 * (
        raw_alpha - alpha_uncertainty_penalty * np.maximum(uncertainty, 0.0)
    )
    current = np.asarray(current_weights, dtype=float)
    benchmark = np.asarray(benchmark_weights, dtype=float)
    linear = np.asarray(
        linear_cost_rates if linear_cost_rates is not None else np.zeros(count)
    )
    impact = np.asarray(
        impact_cost_rates if impact_cost_rates is not None else np.zeros(count)
    )
    adv_limits = np.asarray(
        adv_weight_limits if adv_weight_limits is not None else np.ones(count)
    )
    asset_limits = np.asarray(
        asset_weight_limits
        if asset_weight_limits is not None
        else np.full(count, max_stock_weight),
        dtype=float,
    )
    if asset_limits.shape != (count,):
        raise ValueError("逐证券权重上限维度不一致")

    weights = cp.Variable(count, nonneg=True, name="weights")
    trades = weights - current
    active = weights - benchmark
    portfolio_variance = cp.quad_form(weights, cp.psd_wrap(covariance))
    active_variance = cp.quad_form(active, cp.psd_wrap(covariance))
    expected_return = robust_alpha @ weights
    linear_cost = linear @ cp.abs(trades)
    impact_cost = cp.sum(cp.multiply(impact, cp.square(trades)))
    turnover = cp.norm1(trades)
    objective = cp.Maximize(
        expected_return
        - risk_aversion * portfolio_variance
        - linear_cost
        - impact_cost
        - turnover_penalty * turnover
        - l2_regularization * cp.sum_squares(active)
    )
    constraints: list[cp.Constraint] = [
        weights <= np.minimum(asset_limits, max_stock_weight),
        cp.sum(weights) >= 1.0 - maximum_cash,
        cp.sum(weights) <= 1.0 - minimum_cash,
        cp.abs(trades) <= adv_limits,
        turnover <= max_turnover,
        portfolio_variance <= max_annual_volatility**2 / 252.0,
        active_variance <= max_tracking_error**2 / 252.0,
    ]
    constraint_names = [
        "max_stock_weight",
        "maximum_cash",
        "minimum_cash",
        "adv_trade_limit",
        "max_turnover",
        "max_portfolio_volatility",
        "max_tracking_error",
    ]
    if scenario_returns is not None and max_cvar_loss is not None:
        scenarios = np.asarray(scenario_returns, dtype=float)
        if scenarios.ndim != 2 or scenarios.shape[1] != count:
            raise ValueError("CVaR情景矩阵维度不一致")
        threshold = cp.Variable(name="cvar_var_threshold")
        excess_loss = cp.Variable(
            scenarios.shape[0], nonneg=True, name="cvar_excess_loss"
        )
        losses = -(scenarios @ weights)
        constraints.extend(
            [
                excess_loss >= losses - threshold,
                threshold
                + cp.sum(excess_loss) / ((1.0 - cvar_confidence) * scenarios.shape[0])
                <= max_cvar_loss,
            ]
        )
        constraint_names.extend(["cvar_excess_definition", "max_cvar_loss"])
    if industry_exposures is not None:
        industry = np.asarray(industry_exposures, dtype=float)
        target = np.asarray(benchmark_industry_exposures, dtype=float)
        constraints.append(
            cp.abs(industry.T @ weights - target) <= max_industry_active_weight
        )
        constraint_names.append("industry_active_weight")
    if style_exposures is not None:
        style = np.asarray(style_exposures, dtype=float)
        target = np.asarray(benchmark_style_exposures, dtype=float)
        style_limit = np.asarray(max_style_active_exposure, dtype=float)
        # 风格暴露描述的是股票资产内部结构。有现金仓位时，组合风格和
        # 容忍区间都应按股票仓位归一化；否则求解器按总资产验收、下游
        # 按股票仓位验收，会出现“optimal 后又被清空”的口径矛盾。
        invested_weight = cp.sum(weights)
        constraints.append(
            cp.abs(style.T @ weights - target * invested_weight)
            <= style_limit * invested_weight
        )
        constraint_names.append("style_active_exposure")
    problem = cp.Problem(objective, constraints)
    try:
        problem.solve(solver="CLARABEL")
    except cp.SolverError:
        problem.solve(solver="SCS", eps=1e-6)
    if problem.status not in {cp.OPTIMAL, cp.OPTIMAL_INACCURATE}:
        # 二阶锥在极小协方差数值尺度下，内点法可能误报不可行；
        # 用独立一阶锥求解器复核，两个求解器都失败才输出不可行。
        problem.solve(solver="SCS", eps=1e-6, max_iters=100_000)
    input_payload = {
        "codes": list(codes),
        "alpha": raw_alpha.tolist(),
        "robust_alpha": robust_alpha.tolist(),
        "covariance": covariance.tolist(),
        "current_weights": current.tolist(),
        "benchmark_weights": benchmark.tolist(),
        "asset_weight_limits": asset_limits.tolist(),
        "limits": {
            "max_stock_weight": max_stock_weight,
            "max_tracking_error": max_tracking_error,
            "max_annual_volatility": max_annual_volatility,
            "max_turnover": max_turnover,
            "minimum_cash": minimum_cash,
            "maximum_cash": maximum_cash,
        },
    }
    input_hash = hashlib.sha256(
        json.dumps(input_payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    if problem.status not in {cp.OPTIMAL, cp.OPTIMAL_INACCURATE}:
        return {
            "status": problem.status,
            "passed": False,
            "weights": {},
            "input_sha256": input_hash,
            "infeasibility": diagnose_infeasibility(
                max_stock_weight=max_stock_weight,
                minimum_invested_weight=1.0 - maximum_cash,
                asset_count=count,
                max_turnover=max_turnover,
                current_weights=current,
                adv_weight_limits=adv_limits,
            ),
        }
    solved = np.asarray(weights.value, dtype=float)
    solved_trades = solved - current
    components = {
        "robust_expected_return": float(robust_alpha @ solved),
        "covariance_risk_penalty": float(risk_aversion * solved @ covariance @ solved),
        "linear_cost": float(linear @ np.abs(solved_trades)),
        "impact_cost": float(impact @ np.square(solved_trades)),
        "turnover_penalty": float(turnover_penalty * np.abs(solved_trades).sum()),
        "l2_regularization": float(
            l2_regularization * np.square(solved - benchmark).sum()
        ),
    }
    return {
        "status": problem.status,
        "solver": problem.solver_stats.solver_name,
        "passed": True,
        "weights": dict(zip(codes, solved.tolist(), strict=True)),
        "cash_weight": 1.0 - float(solved.sum()),
        "trades": dict(zip(codes, solved_trades.tolist(), strict=True)),
        "objective_value": float(problem.value),
        "objective_components": components,
        "constraint_slacks": {
            name: np.asarray(constraint.violation()).tolist()
            for name, constraint in zip(constraint_names, constraints, strict=True)
        },
        "dual_values": {
            name: (
                np.asarray(constraint.dual_value).tolist()
                if constraint.dual_value is not None
                else None
            )
            for name, constraint in zip(constraint_names, constraints, strict=True)
        },
        "robust_alpha": dict(zip(codes, robust_alpha.tolist(), strict=True)),
        "alpha_uncertainty": dict(zip(codes, uncertainty.tolist(), strict=True)),
        "input_sha256": input_hash,
        "model_version": "ROBUST_CVX_PORTFOLIO_V1",
    }
