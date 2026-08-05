"""Instrumented PCA / 隐含因子研究 challenger；不进入生产下单链。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import numpy as np

from app.services.linear_alpha_challenger import AlphaRow, FEATURES


@dataclass(frozen=True)
class IpcaFit:
    characteristic_loadings: np.ndarray
    factor_returns: dict[date, np.ndarray]
    characteristic_means: np.ndarray
    characteristic_scales: np.ndarray
    train_r_squared: float
    iterations: int
    converged: bool


def fit_ipca(
    rows: list[AlphaRow],
    *,
    factors: int = 2,
    ridge: float = 1e-4,
    maximum_iterations: int = 100,
    tolerance: float = 1e-8,
) -> IpcaFit:
    dates = sorted({row.signal_date for row in rows})
    if not rows or factors < 1:
        raise ValueError("IPCA 训练数据为空或因子数非法")
    raw_z = np.array(
        [
            [float(row.features.get(name) or 0.0) for name in FEATURES]
            for row in rows
        ]
    )
    means = np.mean(raw_z, axis=0)
    scales = np.std(raw_z, axis=0)
    scales[scales <= 1e-12] = 1.0
    z = (raw_z - means) / scales
    y = np.array([row.forward_return for row in rows])
    row_dates = [row.signal_date for row in rows]
    # 确定性 SVD 初始化，避免随机种子之外的隐含随机性。
    covariance = z.T @ (y[:, None] * z)
    left, _singular, _right = np.linalg.svd(covariance, full_matrices=False)
    gamma = left[:, : min(factors, left.shape[1])]
    if gamma.shape[1] < factors:
        gamma = np.pad(gamma, ((0, 0), (0, factors - gamma.shape[1])))
    factor_returns: dict[date, np.ndarray] = {}
    converged = False
    for iteration in range(1, maximum_iterations + 1):
        for day in dates:
            indices = [index for index, value in enumerate(row_dates) if value == day]
            loadings = z[indices] @ gamma
            target = y[indices]
            factor_returns[day] = np.linalg.pinv(
                loadings.T @ loadings + np.eye(factors) * ridge
            ) @ (loadings.T @ target)
        design_rows = [
            np.kron(factor_returns[row.signal_date], z[index])
            for index, row in enumerate(rows)
        ]
        design = np.array(design_rows)
        vector = np.linalg.pinv(
            design.T @ design + np.eye(design.shape[1]) * ridge
        ) @ (design.T @ y)
        updated = vector.reshape(factors, len(FEATURES)).T
        difference = float(np.max(np.abs(updated - gamma)))
        gamma = updated
        if difference < tolerance:
            converged = True
            break
    fitted = np.array(
        [
            z[index] @ gamma @ factor_returns[row.signal_date]
            for index, row in enumerate(rows)
        ]
    )
    total = float(np.sum((y - float(np.mean(y))) ** 2))
    r_squared = 1.0 - float(np.sum((y - fitted) ** 2)) / total if total > 0 else 0.0
    return IpcaFit(
        characteristic_loadings=gamma,
        factor_returns=factor_returns,
        characteristic_means=means,
        characteristic_scales=scales,
        train_r_squared=r_squared,
        iterations=iteration,
        converged=converged,
    )


def predict_expected_returns(
    fit: IpcaFit,
    rows: list[AlphaRow],
) -> dict[str, float]:
    average_factor = np.mean(
        np.array(list(fit.factor_returns.values())), axis=0
    )
    result: dict[str, float] = {}
    for row in rows:
        raw = np.array(
            [float(row.features.get(name) or 0.0) for name in FEATURES]
        )
        standardized = (
            raw - fit.characteristic_means
        ) / fit.characteristic_scales
        result[row.code] = float(
            standardized @ fit.characteristic_loadings @ average_factor
        )
    return result


def ipca_research_gate(
    *,
    monthly_periods: int,
    oos_r_squared: float | None,
    explicit_factor_oos_r_squared: float | None,
    loading_sign_stability: float | None,
    minimum_periods: int = 60,
) -> dict[str, object]:
    passed = (
        monthly_periods >= minimum_periods
        and oos_r_squared is not None
        and explicit_factor_oos_r_squared is not None
        and oos_r_squared > explicit_factor_oos_r_squared + 0.01
        and loading_sign_stability is not None
        and loading_sign_stability >= 0.80
    )
    return {
        "passed": passed,
        "status": (
            "eligible_for_independent_research_review"
            if passed
            else "challenger_only"
        ),
        "monthly_periods": monthly_periods,
        "minimum_periods": minimum_periods,
        "oos_r_squared": oos_r_squared,
        "explicit_factor_oos_r_squared": explicit_factor_oos_r_squared,
        "loading_sign_stability": loading_sign_stability,
        "production_enabled": False,
    }


def summarize_ipca_fit(fit: IpcaFit) -> dict[str, object]:
    return {
        "status": "challenger_only",
        "factors": fit.characteristic_loadings.shape[1],
        "train_r_squared": fit.train_r_squared,
        "iterations": fit.iterations,
        "converged": fit.converged,
        "characteristic_loadings": {
            feature: fit.characteristic_loadings[index].tolist()
            for index, feature in enumerate(FEATURES)
        },
        "factor_return_means": np.mean(
            np.array(list(fit.factor_returns.values())), axis=0
        ).tolist(),
        "comparison_required": "explicit_industry_style_risk_model_oos_r_squared",
    }
