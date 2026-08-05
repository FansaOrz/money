"""保守情景下的多期组合优化研究原型；不得绕过单期生产门禁。"""

from __future__ import annotations

import cvxpy as cp
import numpy as np


def optimize_multi_period(
    expected_returns: np.ndarray,
    initial_weights: np.ndarray,
    *,
    return_error_bound: float,
    risk_aversion: float = 5.0,
    turnover_penalty: float = 0.01,
) -> dict[str, object]:
    periods, assets = expected_returns.shape
    weights = cp.Variable((periods, assets))
    objective = 0
    constraints = [weights >= 0, cp.sum(weights, axis=1) == 1]
    previous = initial_weights
    for period in range(periods):
        conservative = expected_returns[period] - abs(return_error_bound)
        trade = weights[period] - previous
        objective += (
            conservative @ weights[period]
            - risk_aversion * cp.sum_squares(weights[period])
            - turnover_penalty * cp.norm1(trade)
        )
        previous = weights[period]
    problem = cp.Problem(cp.Maximize(objective), constraints)
    problem.solve(solver=cp.CLARABEL)
    return {
        "status": "challenger",
        "solver_status": problem.status,
        "weights": weights.value.tolist() if weights.value is not None else None,
        "objective": problem.value,
    }
