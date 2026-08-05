"""因子相关、VIF、边际/条件 IC 与冗余治理诊断。"""

from __future__ import annotations

from datetime import date
from statistics import fmean

import numpy as np

from app.services.quant_stats import rank_ic

REDUNDANCY_FACTORS = (
    "momentum_12_1",
    "momentum_6_1",
    "residual_momentum",
    "trend",
    "volatility_60",
    "volatility_120",
    "max_drawdown_120",
    "residual_volatility",
)


def _pearson(left: list[float], right: list[float]) -> float | None:
    if len(left) < 3:
        return None
    x = np.array(left)
    y = np.array(right)
    if float(np.std(x)) <= 1e-15 or float(np.std(y)) <= 1e-15:
        return None
    return float(np.corrcoef(x, y)[0, 1])


def diagnose_factor_redundancy(
    factor_values_by_date: list[
        tuple[date, dict[str, dict[str, float | None]]]
    ],
    forward_returns: list[tuple[date, dict[str, float]]],
    *,
    correlation_threshold: float = 0.85,
) -> dict[str, object]:
    forwards = dict(forward_returns)
    pair_history: dict[str, list[float]] = {}
    vif_history: dict[str, list[float]] = {name: [] for name in REDUNDANCY_FACTORS}
    ic_history: dict[str, list[float]] = {name: [] for name in REDUNDANCY_FACTORS}
    conditional_history: dict[str, list[float]] = {
        name: [] for name in REDUNDANCY_FACTORS
    }
    per_period: list[dict[str, object]] = []
    for signal_date, factor_map in factor_values_by_date:
        returns = forwards.get(signal_date, {})
        period_corr: dict[str, float] = {}
        for left_index, left in enumerate(REDUNDANCY_FACTORS):
            for right in REDUNDANCY_FACTORS[left_index + 1 :]:
                codes = [
                    code
                    for code, value in factor_map.get(left, {}).items()
                    if value is not None
                    and factor_map.get(right, {}).get(code) is not None
                ]
                correlation = _pearson(
                    [float(factor_map[left][code]) for code in codes],
                    [float(factor_map[right][code]) for code in codes],
                )
                if correlation is not None:
                    key = f"{left}|{right}"
                    pair_history.setdefault(key, []).append(correlation)
                    period_corr[key] = correlation
        all_codes = set().union(
            *(set(factor_map.get(name, {})) for name in REDUNDANCY_FACTORS)
        )
        complete_codes = [
            code
            for code in all_codes
            if all(
                factor_map.get(name, {}).get(code) is not None
                for name in REDUNDANCY_FACTORS
            )
        ]
        if len(complete_codes) >= len(REDUNDANCY_FACTORS) + 3:
            matrix = np.array(
                [
                    [float(factor_map[name][code]) for name in REDUNDANCY_FACTORS]
                    for code in complete_codes
                ]
            )
            matrix_std = np.std(matrix, axis=0)
            matrix_std[matrix_std <= 1e-15] = 1.0
            matrix = (matrix - np.mean(matrix, axis=0)) / matrix_std
            for index, factor in enumerate(REDUNDANCY_FACTORS):
                others = np.delete(matrix, index, axis=1)
                target = matrix[:, index]
                design = np.column_stack([np.ones(len(target)), others])
                fitted = design @ (np.linalg.pinv(design) @ target)
                residual = target - fitted
                total = float(np.sum((target - float(np.mean(target))) ** 2))
                r2 = (
                    1.0 - float(np.sum(residual**2)) / total if total > 0 else 0.0
                )
                vif_history[factor].append(
                    1.0 / max(1.0 - min(r2, 0.999999), 1e-6)
                )
                return_values = [returns.get(code) for code in complete_codes]
                valid_indices = [
                    offset
                    for offset, value in enumerate(return_values)
                    if value is not None
                ]
                if len(valid_indices) >= 5:
                    marginal = rank_ic(
                        [target[offset] for offset in valid_indices],
                        [float(return_values[offset]) for offset in valid_indices],
                    )
                    conditional = rank_ic(
                        [residual[offset] for offset in valid_indices],
                        [float(return_values[offset]) for offset in valid_indices],
                    )
                    if marginal is not None:
                        ic_history[factor].append(marginal)
                    if conditional is not None:
                        conditional_history[factor].append(conditional)
        per_period.append(
            {"signal_date": signal_date.isoformat(), "correlations": period_corr}
        )
    correlation_mean = {
        pair: fmean(values) for pair, values in pair_history.items() if values
    }
    vif_mean = {
        factor: (fmean(values) if values else None)
        for factor, values in vif_history.items()
    }
    marginal_ic = {
        factor: (fmean(values) if values else None)
        for factor, values in ic_history.items()
    }
    conditional_ic = {
        factor: (fmean(values) if values else None)
        for factor, values in conditional_history.items()
    }
    actions: list[dict[str, object]] = []
    for pair, correlation in correlation_mean.items():
        if abs(correlation) < correlation_threshold:
            continue
        left, right = pair.split("|")
        left_ic = abs(conditional_ic.get(left) or 0.0)
        right_ic = abs(conditional_ic.get(right) or 0.0)
        actions.append(
            {
                "pair": [left, right],
                "correlation": correlation,
                "action": "orthogonalize_or_drop",
                "weaker_conditional_factor": right if left_ic >= right_ic else left,
                "status": "research_gate",
            }
        )
    return {
        "factors": list(REDUNDANCY_FACTORS),
        "correlation_mean": correlation_mean,
        "vif_mean": vif_mean,
        "marginal_rank_ic_mean": marginal_ic,
        "conditional_rank_ic_mean": conditional_ic,
        "periods": per_period,
        "actions": actions,
        "production_policy": {
            "trend": "cross_sectionally_orthogonalized_to_momentum_12_1",
            "volatility_120": "cross_sectionally_orthogonalized_to_volatility_60",
        },
    }
