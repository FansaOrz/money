"""A股协方差、类 Barra 因子风险和跟踪误差模型。"""

from __future__ import annotations

import math
from collections.abc import Sequence

import numpy as np
from sklearn.covariance import LedoitWolf


def psd_covariance(
    returns: np.ndarray,
    *,
    ewma_decay: float = 0.97,
    shrinkage_weight: float = 0.50,
) -> dict[str, object]:
    """EWMA 与 Ledoit-Wolf 收缩组合，并投影为正半定矩阵。"""
    matrix = np.asarray(returns, dtype=float)
    if matrix.ndim != 2 or matrix.shape[0] < 3 or matrix.shape[1] < 1:
        raise ValueError("协方差估计至少需要3期、1只证券")
    count = matrix.shape[0]
    weights = np.asarray(
        [(1.0 - ewma_decay) * ewma_decay ** (count - 1 - index) for index in range(count)]
    )
    weights /= weights.sum()
    mean = np.sum(matrix * weights[:, None], axis=0)
    centered = matrix - mean
    ewma = (centered * weights[:, None]).T @ centered
    lw = LedoitWolf().fit(matrix)
    covariance = (
        (1.0 - shrinkage_weight) * ewma
        + shrinkage_weight * lw.covariance_
    )
    eigenvalues, eigenvectors = np.linalg.eigh((covariance + covariance.T) / 2.0)
    floor = max(float(eigenvalues.max()) * 1e-10, 1e-12)
    clipped = np.maximum(eigenvalues, floor)
    covariance = eigenvectors @ np.diag(clipped) @ eigenvectors.T
    return {
        "covariance": covariance,
        "ewma_covariance": ewma,
        "ledoit_wolf_covariance": lw.covariance_,
        "ledoit_wolf_shrinkage": float(lw.shrinkage_),
        "minimum_eigenvalue": float(clipped.min()),
        "condition_number": float(clipped.max() / clipped.min()),
        "observations": count,
        "assets": matrix.shape[1],
        "model_version": "EWMA_LED_WOLF_PSD_V1",
    }


def portfolio_risk(
    weights: Sequence[float],
    covariance: np.ndarray,
    *,
    factor_exposures: np.ndarray | None = None,
    factor_covariance: np.ndarray | None = None,
    specific_variances: Sequence[float] | None = None,
    industry_factor_indices: Sequence[int] = (),
) -> dict[str, object]:
    vector = np.asarray(weights, dtype=float)
    covariance = np.asarray(covariance, dtype=float)
    variance = float(vector @ covariance @ vector)
    marginal = covariance @ vector
    component = vector * marginal
    report: dict[str, object] = {
        "total_variance": max(variance, 0.0),
        "total_annualized_volatility": math.sqrt(max(variance, 0.0) * 252.0),
        "asset_variance_contribution": component.tolist(),
    }
    if factor_exposures is not None and factor_covariance is not None:
        exposure = np.asarray(factor_exposures, dtype=float).T @ vector
        factor_component = exposure * (np.asarray(factor_covariance) @ exposure)
        industry = sum(
            factor_component[index]
            for index in industry_factor_indices
            if index < len(factor_component)
        )
        specific = (
            float(np.sum(vector * vector * np.asarray(specific_variances)))
            if specific_variances is not None
            else max(variance - float(factor_component.sum()), 0.0)
        )
        report.update(
            {
                "factor_exposure": exposure.tolist(),
                "factor_variance_contribution": factor_component.tolist(),
                "factor_variance": float(factor_component.sum()),
                "industry_variance": float(industry),
                "specific_variance": specific,
            }
        )
    return report


def estimate_factor_risk_model(
    returns: np.ndarray,
    exposures: np.ndarray,
    *,
    market_caps: np.ndarray | None = None,
    industry_factor_count: int = 0,
    ewma_decay: float = 0.97,
) -> dict[str, object]:
    """每日横截面 WLS 因子收益，再估计因子协方差与特异方差。"""
    returns = np.asarray(returns, dtype=float)
    exposures = np.asarray(exposures, dtype=float)
    if returns.ndim != 2 or exposures.ndim != 2:
        raise ValueError("returns/exposures 必须为二维矩阵")
    if returns.shape[1] != exposures.shape[0]:
        raise ValueError("证券数与暴露矩阵不一致")
    missing_ratio = float(np.isnan(exposures).mean())
    if missing_ratio > 0:
        medians = np.nanmedian(exposures, axis=0)
        exposures = np.where(np.isnan(exposures), medians, exposures)
    caps = (
        np.asarray(market_caps, dtype=float)
        if market_caps is not None
        else np.ones(returns.shape[1])
    )
    weights = np.sqrt(np.maximum(caps, 1.0))
    design = exposures
    weighted_design = design * np.sqrt(weights)[:, None]
    inverse = np.linalg.pinv(weighted_design.T @ weighted_design)
    factor_returns: list[np.ndarray] = []
    residuals: list[np.ndarray] = []
    for cross_section in returns:
        coefficients = (
            inverse @ weighted_design.T @ (cross_section * np.sqrt(weights))
        )
        factor_returns.append(coefficients)
        residuals.append(cross_section - design @ coefficients)
    factor_matrix = np.asarray(factor_returns)
    factor_cov = psd_covariance(
        factor_matrix, ewma_decay=ewma_decay
    )["covariance"]
    residual_matrix = np.asarray(residuals)
    specific = np.maximum(
        np.var(residual_matrix, axis=0, ddof=1),
        1e-10,
    )
    predicted = design @ factor_cov @ design.T + np.diag(specific)
    predicted = psd_covariance(
        np.random.default_rng(20260805).multivariate_normal(
            np.zeros(predicted.shape[0]), predicted, size=max(30, returns.shape[0])
        ),
        shrinkage_weight=0.0,
    )["covariance"]
    return {
        "factor_returns": factor_matrix,
        "factor_covariance": factor_cov,
        "specific_variances": specific,
        "predicted_asset_covariance": predicted,
        "industry_factor_count": industry_factor_count,
        "missing_exposure_ratio": missing_ratio,
        "condition_number": float(np.linalg.cond(predicted)),
        "minimum_eigenvalue": float(np.linalg.eigvalsh(predicted).min()),
        "model_version": "CHINA_EQUITY_FACTOR_RISK_V1",
    }


def tracking_error(
    weights: Sequence[float],
    benchmark_weights: Sequence[float],
    covariance: np.ndarray,
    *,
    realized_strategy_returns: Sequence[float] | None = None,
    realized_benchmark_returns: Sequence[float] | None = None,
) -> dict[str, float | None]:
    active_weights = np.asarray(weights, dtype=float) - np.asarray(
        benchmark_weights, dtype=float
    )
    predicted = math.sqrt(
        max(float(active_weights @ covariance @ active_weights), 0.0) * 252.0
    )
    realized = information_ratio = None
    if realized_strategy_returns is not None and realized_benchmark_returns is not None:
        count = min(
            len(realized_strategy_returns), len(realized_benchmark_returns)
        )
        active = np.asarray(realized_strategy_returns[:count]) - np.asarray(
            realized_benchmark_returns[:count]
        )
        if count >= 2:
            std = float(active.std(ddof=1))
            realized = std * math.sqrt(252.0)
            information_ratio = (
                float(active.mean()) / std * math.sqrt(252.0)
                if std > 0
                else None
            )
    return {
        "predicted_tracking_error": predicted,
        "realized_tracking_error": realized,
        "information_ratio": information_ratio,
        "prediction_bias": (
            predicted - realized if realized is not None else None
        ),
    }


def rolling_risk_calibration(
    predicted: Sequence[float],
    realized: Sequence[float],
) -> dict[str, float | int | None]:
    count = min(len(predicted), len(realized))
    if count == 0:
        return {"observations": 0, "mean_ratio": None, "mean_bias": None}
    ratios = [
        realized[index] / predicted[index]
        for index in range(count)
        if predicted[index] > 0
    ]
    return {
        "observations": count,
        "mean_ratio": float(np.mean(ratios)) if ratios else None,
        "mean_bias": float(
            np.mean(np.asarray(predicted[:count]) - np.asarray(realized[:count]))
        ),
    }
