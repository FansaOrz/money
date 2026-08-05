"""因子 IC 的 Newey-West、块 Bootstrap、有效样本量与 BH-FDR。"""

from __future__ import annotations

import math
import random
from statistics import fmean

from app.services.quant_stats import rank_ic


def _normal_cdf(value: float) -> float:
    return 0.5 * (1.0 + math.erf(value / math.sqrt(2.0)))


def _newey_west(values: list[float]) -> tuple[float | None, float | None]:
    if len(values) < 3:
        return None, None
    mean = fmean(values)
    centered = [value - mean for value in values]
    lag = min(max(1, int(len(values) ** 0.25)), len(values) - 1)
    long_run = sum(value * value for value in centered) / len(values)
    for offset in range(1, lag + 1):
        covariance = sum(
            centered[index] * centered[index - offset]
            for index in range(offset, len(centered))
        ) / len(centered)
        long_run += 2.0 * (1.0 - offset / (lag + 1.0)) * covariance
    standard_error = math.sqrt(max(long_run, 0.0) / len(values))
    return standard_error, mean / standard_error if standard_error > 0 else None


def _block_bootstrap_ci(
    values: list[float],
    *,
    seed: int = 20260805,
    samples: int = 2000,
) -> tuple[float, float] | None:
    if len(values) < 3:
        return None
    generator = random.Random(seed)
    block = max(1, int(math.sqrt(len(values))))
    means: list[float] = []
    for _ in range(samples):
        sampled: list[float] = []
        while len(sampled) < len(values):
            start = generator.randrange(len(values))
            sampled.extend(
                values[(start + offset) % len(values)]
                for offset in range(block)
            )
        means.append(fmean(sampled[: len(values)]))
    means.sort()
    return (
        means[int(0.025 * (len(means) - 1))],
        means[int(0.975 * (len(means) - 1))],
    )


def _effective_sample_size(values: list[float]) -> float:
    if len(values) < 3:
        return float(len(values))
    mean = fmean(values)
    denominator = sum((value - mean) ** 2 for value in values)
    if denominator <= 0:
        return float(len(values))
    autocorrelation = sum(
        (values[index] - mean) * (values[index - 1] - mean)
        for index in range(1, len(values))
    ) / denominator
    autocorrelation = min(max(autocorrelation, -0.99), 0.99)
    return min(
        float(len(values)),
        max(1.0, len(values) * (1.0 - autocorrelation) / (1.0 + autocorrelation)),
    )


def _bh_adjust(pvalues: dict[str, float]) -> dict[str, float]:
    ordered = sorted(pvalues, key=pvalues.get)
    count = len(ordered)
    adjusted: dict[str, float] = {}
    running = 1.0
    for reverse_index, name in enumerate(reversed(ordered), start=1):
        rank = count - reverse_index + 1
        candidate = min(pvalues[name] * count / rank, 1.0)
        running = min(running, candidate)
        adjusted[name] = running
    return adjusted


def factor_ic_significance(
    factor_values_by_date: list[
        tuple[object, dict[str, dict[str, float | None]]]
    ],
    forward_returns: list[tuple[object, dict[str, float]]],
    *,
    extra_attempt_pvalues: dict[str, float] | None = None,
) -> dict[str, object]:
    forwards = dict(forward_returns)
    ic_series: dict[str, list[float]] = {}
    for signal_date, factors in factor_values_by_date:
        returns = forwards.get(signal_date, {})
        for factor, values in factors.items():
            codes = [
                code
                for code, value in values.items()
                if value is not None and code in returns
            ]
            if len(codes) < 5:
                continue
            ic = rank_ic(
                [float(values[code]) for code in codes],
                [returns[code] for code in codes],
            )
            if ic is not None:
                ic_series.setdefault(factor, []).append(ic)
    reports: dict[str, dict[str, object]] = {}
    pvalues: dict[str, float] = {}
    for factor, values in ic_series.items():
        mean = fmean(values)
        standard_error, t_stat = _newey_west(values)
        p_value = (
            2.0 * (1.0 - _normal_cdf(abs(t_stat)))
            if t_stat is not None
            else 1.0
        )
        pvalues[factor] = p_value
        std = (
            math.sqrt(
                sum((value - mean) ** 2 for value in values)
                / (len(values) - 1)
            )
            if len(values) >= 2
            else None
        )
        reports[factor] = {
            "observations": len(values),
            "effective_observations": _effective_sample_size(values),
            "mean": mean,
            "std": std,
            "icir": mean / std if std not in (None, 0.0) else None,
            "newey_west_standard_error": standard_error,
            "newey_west_t": t_stat,
            "p_value": p_value,
            "block_bootstrap_95_ci": _block_bootstrap_ci(values),
            "series": values,
        }
    pvalues.update(extra_attempt_pvalues or {})
    adjusted = _bh_adjust(pvalues)
    for factor, report in reports.items():
        ci = report["block_bootstrap_95_ci"]
        report["fdr_q_value"] = adjusted.get(factor)
        report["proven_positive"] = bool(
            ci
            and ci[0] > 0  # type: ignore[index]
            and adjusted.get(factor, 1.0) <= 0.10
        )
    return {
        "factors": reports,
        "all_adjusted_pvalues": adjusted,
        "tested_hypotheses": len(pvalues),
        "status": (
            "alpha_evidence_sufficient"
            if reports
            and reports.get("composite", {}).get("proven_positive") is True
            else "alpha_evidence_insufficient"
        ),
    }
