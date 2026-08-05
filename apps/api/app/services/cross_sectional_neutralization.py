"""行业+连续风格控制的稳健横截面 WLS 残差化。"""

from __future__ import annotations

from dataclasses import dataclass
from math import isfinite, sqrt

import numpy as np


@dataclass(frozen=True)
class NeutralizationObservation:
    code: str
    industry: str
    value: float | None
    log_market_cap: float | None
    beta: float | None
    liquidity: float | None
    float_market_cap: float | None


@dataclass(frozen=True)
class NeutralizationResult:
    residuals: dict[str, float | None]
    coefficients: dict[str, float]
    r_squared: float | None
    weighted_control_correlations: dict[str, float | None]
    method: str
    sample_size: int
    small_industries: tuple[str, ...]


def _weighted_correlation(
    left: np.ndarray,
    right: np.ndarray,
    weights: np.ndarray,
) -> float | None:
    weight_sum = float(weights.sum())
    if weight_sum <= 0:
        return None
    left_mean = float(np.sum(weights * left) / weight_sum)
    right_mean = float(np.sum(weights * right) / weight_sum)
    left_centered = left - left_mean
    right_centered = right - right_mean
    denominator = sqrt(
        float(np.sum(weights * left_centered**2))
        * float(np.sum(weights * right_centered**2))
    )
    if denominator <= 1e-15:
        return None
    return float(np.sum(weights * left_centered * right_centered) / denominator)


def neutralize_wls(
    observations: list[NeutralizationObservation],
    *,
    minimum_sample: int = 20,
    minimum_industry_size: int = 5,
    ridge: float = 1e-10,
) -> NeutralizationResult:
    """对有效因子值执行 WLS；控制缺失以中位数填充并带缺失哑变量。"""
    residuals = {item.code: None for item in observations}
    usable = [
        item
        for item in observations
        if item.value is not None and isfinite(float(item.value))
    ]
    if len(usable) < minimum_sample:
        return NeutralizationResult(
            residuals=residuals,
            coefficients={},
            r_squared=None,
            weighted_control_correlations={},
            method="fallback_small_cross_section",
            sample_size=len(usable),
            small_industries=(),
        )
    industry_counts: dict[str, int] = {}
    for item in usable:
        industry_counts[item.industry] = industry_counts.get(item.industry, 0) + 1
    small_industries = {
        industry
        for industry, count in industry_counts.items()
        if count < minimum_industry_size
    }
    industries = [
        "其他小行业" if item.industry in small_industries else item.industry
        for item in usable
    ]
    distinct_industries = sorted(set(industries))
    reference = distinct_industries[0]
    control_names = ("log_market_cap", "beta", "liquidity")
    raw_controls: dict[str, list[float | None]] = {
        name: [getattr(item, name) for item in usable] for name in control_names
    }
    normalized_controls: dict[str, np.ndarray] = {}
    missing_controls: dict[str, np.ndarray] = {}
    for name, raw in raw_controls.items():
        valid = [float(value) for value in raw if value is not None and isfinite(value)]
        fill = float(np.median(valid)) if valid else 0.0
        missing = np.array(
            [value is None or not isfinite(float(value)) for value in raw],
            dtype=float,
        )
        filled = np.array(
            [
                fill
                if value is None or not isfinite(float(value))
                else float(value)
                for value in raw
            ],
            dtype=float,
        )
        std = float(np.std(filled))
        normalized_controls[name] = (
            (filled - float(np.mean(filled))) / std if std > 0 else filled * 0.0
        )
        missing_controls[name] = missing

    columns: list[np.ndarray] = [np.ones(len(usable))]
    names = ["intercept"]
    for industry in distinct_industries:
        if industry == reference:
            continue
        columns.append(
            np.array([1.0 if value == industry else 0.0 for value in industries])
        )
        names.append(f"industry[{industry}]")
    for name in control_names:
        columns.append(normalized_controls[name])
        names.append(name)
        if float(missing_controls[name].sum()) > 0:
            columns.append(missing_controls[name])
            names.append(f"{name}_missing")
    design = np.column_stack(columns)
    response = np.array([float(item.value) for item in usable])
    caps = [
        float(item.float_market_cap)
        for item in usable
        if item.float_market_cap is not None
        and isfinite(float(item.float_market_cap))
        and float(item.float_market_cap) > 0
    ]
    cap_fill = float(np.median(caps)) if caps else 1.0
    raw_weights = np.array(
        [
            sqrt(
                float(item.float_market_cap)
                if item.float_market_cap is not None
                and isfinite(float(item.float_market_cap))
                and float(item.float_market_cap) > 0
                else cap_fill
            )
            for item in usable
        ]
    )
    lower, upper = np.quantile(raw_weights, [0.01, 0.99])
    weights = np.clip(raw_weights, lower, upper)
    weights /= float(np.mean(weights))
    root_weight = np.sqrt(weights)
    weighted_design = design * root_weight[:, None]
    weighted_response = response * root_weight
    penalty = np.eye(design.shape[1]) * ridge
    penalty[0, 0] = 0.0
    coefficients = np.linalg.pinv(
        weighted_design.T @ weighted_design + penalty
    ) @ (weighted_design.T @ weighted_response)
    fitted = design @ coefficients
    residual = response - fitted
    for item, value in zip(usable, residual, strict=True):
        residuals[item.code] = float(value)
    response_mean = float(np.sum(weights * response) / np.sum(weights))
    total = float(np.sum(weights * (response - response_mean) ** 2))
    unexplained = float(np.sum(weights * residual**2))
    r_squared = 1.0 - unexplained / total if total > 1e-15 else None
    correlations = {
        name: _weighted_correlation(residual, normalized_controls[name], weights)
        for name in control_names
    }
    return NeutralizationResult(
        residuals=residuals,
        coefficients={
            name: float(value) for name, value in zip(names, coefficients, strict=True)
        },
        r_squared=r_squared,
        weighted_control_correlations=correlations,
        method="wls_sqrt_float_market_cap",
        sample_size=len(usable),
        small_industries=tuple(sorted(small_industries)),
    )
