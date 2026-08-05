"""LightGBM/XGBoost 排序 challenger 的前置门禁、时间切分与晋级规则。"""

from __future__ import annotations

from dataclasses import dataclass
from statistics import fmean
from typing import Iterable

import numpy as np

from app.services.linear_alpha_challenger import AlphaRow, FEATURES
from app.services.quant_stats import rank_ic


@dataclass(frozen=True)
class TreePrerequisites:
    pit_ready: bool
    nested_time_validation_ready: bool
    experiment_registered: bool
    simulator_validated: bool


def prerequisite_gate(prerequisites: TreePrerequisites) -> tuple[bool, list[str]]:
    mapping = {
        "PIT 数据未通过": prerequisites.pit_ready,
        "嵌套时间验证未就绪": prerequisites.nested_time_validation_ready,
        "实验未预注册": prerequisites.experiment_registered,
        "模拟器尚未通过运行有效性验证": prerequisites.simulator_validated,
    }
    reasons = [reason for reason, passed in mapping.items() if not passed]
    return not reasons, reasons


def promotion_gate(
    independent_windows: Iterable[dict[str, float]],
    *,
    minimum_windows: int = 3,
    minimum_win_rate: float = 2 / 3,
    minimum_mean_net_ic_improvement: float = 0.005,
) -> dict[str, object]:
    windows = list(independent_windows)
    improvements = [
        float(item["tree_net_rank_ic"]) - float(item["ridge_net_rank_ic"])
        for item in windows
    ]
    win_rate = (
        sum(value > 0 for value in improvements) / len(improvements)
        if improvements
        else 0.0
    )
    mean_improvement = fmean(improvements) if improvements else None
    passed = (
        len(windows) >= minimum_windows
        and win_rate >= minimum_win_rate
        and mean_improvement is not None
        and mean_improvement >= minimum_mean_net_ic_improvement
    )
    return {
        "passed": passed,
        "status": (
            "eligible_for_independent_model_risk_review"
            if passed
            else "challenger_only"
        ),
        "windows": len(windows),
        "win_rate": win_rate,
        "mean_net_rank_ic_improvement": mean_improvement,
        "requirements": {
            "minimum_windows": minimum_windows,
            "minimum_win_rate": minimum_win_rate,
            "minimum_mean_net_ic_improvement": (
                minimum_mean_net_ic_improvement
            ),
        },
    }


def run_xgboost_rank_challenger(
    rows: list[AlphaRow],
    *,
    prerequisites: TreePrerequisites,
    minimum_training_periods: int = 18,
) -> dict[str, object]:
    """严格按时间滚动训练；验证段早停；输出永远先保持 challenger。"""
    allowed, reasons = prerequisite_gate(prerequisites)
    if not allowed:
        return {"status": "blocked_prerequisites", "reasons": reasons}
    try:
        from xgboost import XGBRanker
    except ImportError:
        return {
            "status": "optional_backend_unavailable",
            "backend": "xgboost",
            "install_extra": "money-api[ml]",
        }
    dates = sorted({row.signal_date for row in rows})
    predictions: list[dict[str, object]] = []
    window_metrics: list[dict[str, float]] = []
    for position in range(minimum_training_periods, len(dates)):
        prediction_date = dates[position]
        earlier = dates[:position]
        validation_count = max(3, len(earlier) // 5)
        train_dates = set(earlier[:-validation_count])
        validation_dates = set(earlier[-validation_count:])
        train = [row for row in rows if row.signal_date in train_dates]
        validation = [row for row in rows if row.signal_date in validation_dates]
        test = [row for row in rows if row.signal_date == prediction_date]
        if not train or not validation or len(test) < 5:
            continue

        def matrix(selected: list[AlphaRow]) -> np.ndarray:
            return np.array(
                [
                    [float(row.features.get(name) or 0.0) for name in FEATURES]
                    for row in selected
                ]
            )

        # 横截面 rank 目标减少不同月份收益尺度差异。
        def rank_target(selected: list[AlphaRow]) -> np.ndarray:
            result = np.zeros(len(selected))
            by_date: dict[object, list[int]] = {}
            for index, row in enumerate(selected):
                by_date.setdefault(row.signal_date, []).append(index)
            for indices in by_date.values():
                ordered = sorted(indices, key=lambda index: selected[index].forward_return)
                for rank, index in enumerate(ordered):
                    result[index] = rank / max(len(ordered) - 1, 1)
            return result

        def groups(selected: list[AlphaRow]) -> list[int]:
            counts: dict[object, int] = {}
            for row in selected:
                counts[row.signal_date] = counts.get(row.signal_date, 0) + 1
            return [counts[day] for day in sorted(counts)]

        model = XGBRanker(
            objective="rank:pairwise",
            n_estimators=1000,
            learning_rate=0.03,
            max_depth=3,
            min_child_weight=20,
            subsample=0.8,
            colsample_bytree=0.8,
            reg_alpha=1.0,
            reg_lambda=10.0,
            random_state=20260805,
            n_jobs=1,
            early_stopping_rounds=50,
        )
        model.fit(
            matrix(train),
            rank_target(train),
            group=groups(train),
            eval_set=[(matrix(validation), rank_target(validation))],
            eval_group=[groups(validation)],
            verbose=False,
        )
        predicted = model.predict(matrix(test))
        actual = [row.forward_return for row in test]
        tree_ic = rank_ic(predicted.tolist(), actual)
        baseline_rows = [row for row in test if row.baseline_score is not None]
        ridge_or_rule_ic = rank_ic(
            [float(row.baseline_score) for row in baseline_rows],
            [row.forward_return for row in baseline_rows],
        )
        if tree_ic is not None and ridge_or_rule_ic is not None:
            window_metrics.append(
                {
                    "tree_net_rank_ic": tree_ic,
                    "ridge_net_rank_ic": ridge_or_rule_ic,
                }
            )
        for row, value in zip(test, predicted, strict=True):
            predictions.append(
                {
                    "signal_date": prediction_date.isoformat(),
                    "code": row.code,
                    "prediction": float(value),
                    "actual_return": row.forward_return,
                }
            )
    return {
        "status": "challenger_only",
        "backend": "xgboost",
        "objective": "rank:pairwise",
        "time_split": "expanding_train_then_validation_early_stop_then_oos",
        "predictions": predictions,
        "window_metrics": window_metrics,
        "promotion_gate": promotion_gate(window_metrics),
        "required_comparators": [
            "ridge_elastic_net",
            "shrunk_ic_weighting",
            "equal_weight_factor_baseline",
        ],
    }
