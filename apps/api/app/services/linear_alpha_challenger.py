"""Ridge/Elastic-Net 横截面收益预测基线（研究 challenger，不直接下单）。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from math import sqrt
from statistics import fmean

import numpy as np

from app.services.quant_stats import rank_ic

FEATURES = ("quality", "value", "momentum", "trend", "lowvol")


@dataclass(frozen=True)
class AlphaRow:
    signal_date: date
    code: str
    industry: str
    features: dict[str, float | None]
    forward_return: float
    baseline_score: float | None = None


def _soft_threshold(value: float, threshold: float) -> float:
    return max(value - threshold, 0.0) - max(-value - threshold, 0.0)


def _fit_elastic_net(
    matrix: np.ndarray,
    target: np.ndarray,
    *,
    alpha: float,
    l1_ratio: float,
    iterations: int = 2000,
    tolerance: float = 1e-10,
) -> np.ndarray:
    if l1_ratio <= 0:
        penalty = np.eye(matrix.shape[1]) * alpha
        penalty[0, 0] = 0.0
        return np.linalg.pinv(matrix.T @ matrix + penalty) @ (matrix.T @ target)
    coefficients = np.zeros(matrix.shape[1])
    column_norms = np.sum(matrix**2, axis=0)
    for _ in range(iterations):
        previous = coefficients.copy()
        for column in range(matrix.shape[1]):
            partial = target - matrix @ coefficients + matrix[:, column] * coefficients[column]
            raw = float(matrix[:, column] @ partial)
            if column == 0:
                coefficients[column] = raw / max(column_norms[column], 1e-15)
            else:
                coefficients[column] = _soft_threshold(
                    raw, alpha * l1_ratio
                ) / max(column_norms[column] + alpha * (1.0 - l1_ratio), 1e-15)
        if float(np.max(np.abs(coefficients - previous))) < tolerance:
            break
    return coefficients


def _industry_residual_targets(rows: list[AlphaRow]) -> dict[tuple[date, str], float]:
    groups: dict[tuple[date, str], list[float]] = {}
    for row in rows:
        groups.setdefault((row.signal_date, row.industry), []).append(
            row.forward_return
        )
    means = {key: fmean(values) for key, values in groups.items()}
    return {
        (row.signal_date, row.code): (
            row.forward_return - means[(row.signal_date, row.industry)]
        )
        for row in rows
    }


def _design(
    rows: list[AlphaRow],
    *,
    means: np.ndarray | None = None,
    scales: np.ndarray | None = None,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    raw = np.array(
        [
            [
                float(row.features.get(feature) or 0.0)
                for feature in FEATURES
            ]
            for row in rows
        ],
        dtype=float,
    )
    fitted_means = np.mean(raw, axis=0) if means is None else means
    fitted_scales = np.std(raw, axis=0) if scales is None else scales
    fitted_scales = fitted_scales.copy()
    fitted_scales[fitted_scales <= 1e-12] = 1.0
    standardized = (raw - fitted_means) / fitted_scales
    return (
        np.column_stack([np.ones(len(rows)), standardized]),
        fitted_means,
        fitted_scales,
    )


def walk_forward_linear_challenger(
    rows: list[AlphaRow],
    *,
    minimum_training_periods: int = 12,
    alphas: tuple[float, ...] = (0.01, 0.1, 1.0),
    l1_ratios: tuple[float, ...] = (0.0, 0.5),
) -> dict[str, object]:
    """每个预测日只用更早标签，并在训练段尾部嵌套选择超参数。"""
    targets = _industry_residual_targets(rows)
    dates = sorted({row.signal_date for row in rows})
    predictions: list[dict[str, object]] = []
    coefficient_history: list[dict[str, object]] = []
    for position, prediction_date in enumerate(dates):
        training_dates = dates[:position]
        if len(training_dates) < minimum_training_periods:
            continue
        validation_count = max(3, len(training_dates) // 5)
        inner_train_dates = set(training_dates[:-validation_count])
        validation_dates = set(training_dates[-validation_count:])
        inner_rows = [row for row in rows if row.signal_date in inner_train_dates]
        validation_rows = [
            row for row in rows if row.signal_date in validation_dates
        ]
        if not inner_rows or not validation_rows:
            continue
        inner_x, means, scales = _design(inner_rows)
        inner_y = np.array(
            [targets[(row.signal_date, row.code)] for row in inner_rows]
        )
        validation_x, _means, _scales = _design(
            validation_rows, means=means, scales=scales
        )
        validation_y = np.array(
            [targets[(row.signal_date, row.code)] for row in validation_rows]
        )
        candidates: list[tuple[float, float, float]] = []
        for alpha in alphas:
            for l1_ratio in l1_ratios:
                coefficients = _fit_elastic_net(
                    inner_x,
                    inner_y,
                    alpha=alpha,
                    l1_ratio=l1_ratio,
                )
                mse = float(np.mean((validation_y - validation_x @ coefficients) ** 2))
                candidates.append((mse, alpha, l1_ratio))
        _mse, selected_alpha, selected_l1 = min(candidates)
        training_rows = [
            row for row in rows if row.signal_date in set(training_dates)
        ]
        train_x, means, scales = _design(training_rows)
        train_y = np.array(
            [targets[(row.signal_date, row.code)] for row in training_rows]
        )
        coefficients = _fit_elastic_net(
            train_x,
            train_y,
            alpha=selected_alpha,
            l1_ratio=selected_l1,
        )
        test_rows = [row for row in rows if row.signal_date == prediction_date]
        test_x, _means, _scales = _design(test_rows, means=means, scales=scales)
        predicted = test_x @ coefficients
        for row, value in zip(test_rows, predicted, strict=True):
            predictions.append(
                {
                    "signal_date": prediction_date.isoformat(),
                    "code": row.code,
                    "prediction": float(value),
                    "actual_residual_return": targets[
                        (row.signal_date, row.code)
                    ],
                    "baseline_score": row.baseline_score,
                }
            )
        coefficient_history.append(
            {
                "prediction_date": prediction_date.isoformat(),
                "training_start": training_dates[0].isoformat(),
                "training_end": training_dates[-1].isoformat(),
                "validation_dates": [
                    day.isoformat() for day in sorted(validation_dates)
                ],
                "alpha": selected_alpha,
                "l1_ratio": selected_l1,
                "coefficients": {
                    "intercept": float(coefficients[0]),
                    **{
                        feature: float(coefficients[index + 1])
                        for index, feature in enumerate(FEATURES)
                    },
                },
                "feature_means": dict(zip(FEATURES, means.tolist(), strict=True)),
                "feature_scales": dict(zip(FEATURES, scales.tolist(), strict=True)),
            }
        )
    by_date: dict[str, list[dict[str, object]]] = {}
    for item in predictions:
        by_date.setdefault(str(item["signal_date"]), []).append(item)
    challenger_ics: list[float] = []
    baseline_ics: list[float] = []
    for items in by_date.values():
        challenger = rank_ic(
            [float(item["prediction"]) for item in items],
            [float(item["actual_residual_return"]) for item in items],
        )
        baseline_items = [
            item for item in items if item["baseline_score"] is not None
        ]
        baseline = rank_ic(
            [float(item["baseline_score"]) for item in baseline_items],
            [float(item["actual_residual_return"]) for item in baseline_items],
        )
        if challenger is not None:
            challenger_ics.append(challenger)
        if baseline is not None:
            baseline_ics.append(baseline)
    ordered = sorted(predictions, key=lambda item: float(item["prediction"]))
    calibration: list[dict[str, object]] = []
    if ordered:
        for bucket in range(10):
            start = len(ordered) * bucket // 10
            end = len(ordered) * (bucket + 1) // 10
            selected = ordered[start:end]
            if selected:
                calibration.append(
                    {
                        "decile": bucket + 1,
                        "count": len(selected),
                        "mean_prediction": fmean(
                            float(item["prediction"]) for item in selected
                        ),
                        "mean_actual": fmean(
                            float(item["actual_residual_return"])
                            for item in selected
                        ),
                    }
                )
    stability: dict[str, dict[str, float | None]] = {}
    for feature in FEATURES:
        values = [
            float(item["coefficients"][feature])  # type: ignore[index]
            for item in coefficient_history
        ]
        stability[feature] = {
            "mean": fmean(values) if values else None,
            "std": (
                sqrt(
                    sum((value - fmean(values)) ** 2 for value in values)
                    / (len(values) - 1)
                )
                if len(values) >= 2
                else None
            ),
            "sign_stability": (
                max(
                    sum(value >= 0 for value in values),
                    sum(value < 0 for value in values),
                )
                / len(values)
                if values
                else None
            ),
        }
    return {
        "status": "challenger_only",
        "target": "next_holding_period_industry_residual_return",
        "features": list(FEATURES),
        "predictions": predictions,
        "coefficient_history": coefficient_history,
        "coefficient_stability": stability,
        "calibration_deciles": calibration,
        "oos_rank_ic_mean": (
            fmean(challenger_ics) if challenger_ics else None
        ),
        "fixed_weight_baseline_rank_ic_mean": (
            fmean(baseline_ics) if baseline_ics else None
        ),
        "oos_periods": len(challenger_ics),
    }


def rows_from_backtest(outcome: object) -> list[AlphaRow]:
    factor_dates = dict(getattr(outcome, "factor_values_by_date", []))
    forwards = dict(getattr(outcome, "forward_returns", []))
    scores = dict(getattr(outcome, "scores_by_date", []))
    groups = dict(getattr(outcome, "groups_by_date", []))
    rows: list[AlphaRow] = []
    for signal_date, returns in forwards.items():
        factor_map = factor_dates.get(signal_date, {})
        for code, forward_return in returns.items():
            rows.append(
                AlphaRow(
                    signal_date=signal_date,
                    code=code,
                    industry=groups.get(signal_date, {}).get(
                        code, ("未知", "unknown")
                    )[0],
                    features={
                        feature: factor_map.get(feature, {}).get(code)
                        for feature in FEATURES
                    },
                    forward_return=forward_return,
                    baseline_score=scores.get(signal_date, {}).get(code),
                )
            )
    return rows
