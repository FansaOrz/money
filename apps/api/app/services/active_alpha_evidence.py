"""相对基准主动收益与回归 Alpha 的稳健统计证据。"""

from __future__ import annotations

import math
import random
from collections.abc import Mapping, Sequence
from statistics import fmean

import numpy as np


def _newey_west_mean(values: Sequence[float]) -> tuple[float | None, float | None]:
    if len(values) < 3:
        return None, None
    centered = np.asarray(values, dtype=float) - fmean(values)
    lag = min(max(1, int(len(values) ** 0.25)), len(values) - 1)
    long_run = float(centered @ centered) / len(values)
    for offset in range(1, lag + 1):
        covariance = float(centered[offset:] @ centered[:-offset]) / len(values)
        long_run += 2.0 * (1.0 - offset / (lag + 1.0)) * covariance
    standard_error = math.sqrt(max(long_run, 0.0) / len(values))
    return standard_error, fmean(values) / standard_error if standard_error > 0 else None


def _moving_block_indices(
    count: int,
    *,
    samples: int,
    seed: int,
) -> list[list[int]]:
    generator = random.Random(seed)
    block = max(1, int(math.sqrt(count)))
    result: list[list[int]] = []
    for _ in range(samples):
        indices: list[int] = []
        while len(indices) < count:
            start = generator.randrange(count)
            indices.extend((start + offset) % count for offset in range(block))
        result.append(indices[:count])
    return result


def _percentile_95(values: list[float]) -> list[float] | None:
    if not values:
        return None
    ordered = sorted(values)
    return [
        ordered[int(0.025 * (len(ordered) - 1))],
        ordered[int(0.975 * (len(ordered) - 1))],
    ]


def _ols_alpha(
    strategy: np.ndarray,
    factors: np.ndarray,
) -> tuple[float, float | None, float | None, list[float]]:
    design = np.column_stack([np.ones(len(strategy)), factors])
    coefficients = np.linalg.pinv(design.T @ design) @ design.T @ strategy
    residuals = strategy - design @ coefficients
    count, parameter_count = design.shape
    if count <= parameter_count:
        return float(coefficients[0]), None, None, coefficients[1:].tolist()
    lag = min(max(1, int(count ** 0.25)), count - 1)
    meat = np.zeros((parameter_count, parameter_count), dtype=float)
    for index in range(count):
        vector = design[index] * residuals[index]
        meat += np.outer(vector, vector)
    for offset in range(1, lag + 1):
        weight = 1.0 - offset / (lag + 1.0)
        covariance = np.zeros_like(meat)
        for index in range(offset, count):
            current = design[index] * residuals[index]
            previous = design[index - offset] * residuals[index - offset]
            covariance += np.outer(current, previous)
        meat += weight * (covariance + covariance.T)
    inverse = np.linalg.pinv(design.T @ design)
    covariance = inverse @ meat @ inverse
    standard_error = math.sqrt(max(float(covariance[0, 0]), 0.0))
    t_stat = float(coefficients[0]) / standard_error if standard_error > 0 else None
    return float(coefficients[0]), standard_error, t_stat, coefficients[1:].tolist()


def active_alpha_evidence(
    strategy_returns: Sequence[float],
    benchmark_returns: Sequence[float],
    *,
    style_factor_returns: Mapping[str, Sequence[float]] | None = None,
    bootstrap_samples: int = 2000,
    seed: int = 20260805,
    minimum_information_ratio: float = 0.30,
) -> dict[str, object]:
    """计算主动收益、HAC 显著性、块 Bootstrap 与市场/风格回归净 Alpha。"""
    lengths = [len(strategy_returns), len(benchmark_returns)]
    lengths.extend(len(values) for values in (style_factor_returns or {}).values())
    count = min(lengths) if lengths else 0
    if count < 20:
        return {
            "status": "insufficient_observations",
            "passed": False,
            "observations": count,
        }
    strategy = np.asarray(strategy_returns[:count], dtype=float)
    benchmark = np.asarray(benchmark_returns[:count], dtype=float)
    active = strategy - benchmark
    active_mean = float(active.mean())
    active_standard_deviation = float(active.std(ddof=1))
    information_ratio = (
        active_mean / active_standard_deviation * math.sqrt(252.0)
        if active_standard_deviation > 0
        else None
    )
    mean_se, mean_t = _newey_west_mean(active.tolist())

    factor_names = ["market"]
    factor_columns = [benchmark]
    for name in sorted(style_factor_returns or {}):
        factor_names.append(name)
        factor_columns.append(
            np.asarray((style_factor_returns or {})[name][:count], dtype=float)
        )
    factors = np.column_stack(factor_columns)
    alpha, alpha_se, alpha_t, factor_betas = _ols_alpha(strategy, factors)

    active_bootstrap: list[float] = []
    alpha_bootstrap: list[float] = []
    for indices in _moving_block_indices(
        count, samples=bootstrap_samples, seed=seed
    ):
        sample = np.asarray(indices, dtype=int)
        active_bootstrap.append(float(active[sample].mean()))
        sampled_alpha, _, _, _ = _ols_alpha(strategy[sample], factors[sample])
        alpha_bootstrap.append(sampled_alpha)
    active_ci = _percentile_95(active_bootstrap)
    alpha_ci = _percentile_95(alpha_bootstrap)
    failures: list[str] = []
    if active_ci is None or active_ci[0] <= 0:
        failures.append("主动日收益的块Bootstrap区间未严格高于零")
    if alpha_ci is None or alpha_ci[0] <= 0:
        failures.append("市场/风格回归日Alpha区间未严格高于零")
    if information_ratio is None or information_ratio < minimum_information_ratio:
        failures.append("信息比率未达到预注册门槛")
    return {
        "observations": count,
        "active_mean_daily": active_mean,
        "active_annualized_mean": active_mean * 252.0,
        "active_newey_west_standard_error": mean_se,
        "active_newey_west_t": mean_t,
        "active_block_bootstrap_95_ci": active_ci,
        "tracking_error": active_standard_deviation * math.sqrt(252.0),
        "information_ratio": information_ratio,
        "regression_factor_names": factor_names,
        "regression_betas": dict(zip(factor_names, factor_betas, strict=True)),
        "regression_alpha_daily": alpha,
        "regression_alpha_annualized": alpha * 252.0,
        "regression_alpha_newey_west_standard_error": alpha_se,
        "regression_alpha_newey_west_t": alpha_t,
        "regression_alpha_block_bootstrap_95_ci": alpha_ci,
        "failures": failures,
        "passed": not failures,
        "status": "passed" if not failures else "failed",
    }
