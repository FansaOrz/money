"""CSCV/CPCV 试验矩阵与 Probability of Backtest Overfitting (PBO)。"""

from __future__ import annotations

import hashlib
import itertools
import json
import math
from statistics import fmean

PBO_VERSION = "CSCV_PBO_V1"


def cscv_pbo(
    performance_by_trial: dict[str, list[float]],
    *,
    maximum_splits: int = 10_000,
) -> dict[str, object]:
    if not performance_by_trial:
        return {
            "status": "insufficient",
            "probability_backtest_overfitting": None,
            "reason": "试验矩阵为空",
            "version": PBO_VERSION,
        }
    lengths = {len(values) for values in performance_by_trial.values()}
    if len(lengths) != 1 or next(iter(lengths)) < 3:
        return {
            "status": "insufficient",
            "probability_backtest_overfitting": None,
            "reason": "CSCV 至少需要3个等长时期",
            "version": PBO_VERSION,
        }
    trials = sorted(performance_by_trial)
    matrix = [performance_by_trial[trial] for trial in trials]
    periods = len(matrix[0])
    train_size = periods // 2
    combinations = list(itertools.combinations(range(periods), train_size))
    if len(combinations) > maximum_splits:
        step = len(combinations) / maximum_splits
        combinations = [
            combinations[min(int(index * step), len(combinations) - 1)]
            for index in range(maximum_splits)
        ]
    splits: list[dict[str, object]] = []
    overfit = 0
    degradations: list[float] = []
    logits: list[float] = []
    all_indices = set(range(periods))
    for train_indices_tuple in combinations:
        train_indices = set(train_indices_tuple)
        test_indices = sorted(all_indices - train_indices)
        in_sample = [
            fmean(matrix[index][period] for period in train_indices)
            for index in range(len(trials))
        ]
        selected = max(
            range(len(trials)),
            key=lambda index: (in_sample[index], trials[index]),
        )
        out_sample = [
            fmean(matrix[index][period] for period in test_indices)
            for index in range(len(trials))
        ]
        selected_oos = out_sample[selected]
        # 相对秩 1=最好；并列取中位秩。
        below = sum(value < selected_oos for value in out_sample)
        equal = sum(value == selected_oos for value in out_sample)
        relative_rank = min(
            (below + 0.5 * (equal + 1)) / len(out_sample),
            1.0,
        )
        clipped = min(max(relative_rank, 1e-9), 1.0 - 1e-9)
        logit = math.log(clipped / (1.0 - clipped))
        logits.append(logit)
        is_overfit = relative_rank < 0.5
        overfit += int(is_overfit)
        degradation = selected_oos - in_sample[selected]
        degradations.append(degradation)
        splits.append(
            {
                "train_periods": sorted(train_indices),
                "test_periods": test_indices,
                "selected_trial": trials[selected],
                "selected_is_performance": in_sample[selected],
                "selected_oos_performance": selected_oos,
                "oos_relative_rank": relative_rank,
                "logit": logit,
                "overfit": is_overfit,
            }
        )
    canonical = json.dumps(
        {"trials": trials, "matrix": matrix},
        separators=(",", ":"),
        sort_keys=True,
    )
    return {
        "status": "success",
        "version": PBO_VERSION,
        "trial_count": len(trials),
        "period_count": periods,
        "split_count": len(splits),
        "probability_backtest_overfitting": overfit / len(splits),
        "mean_oos_degradation": fmean(degradations),
        "median_logit": sorted(logits)[len(logits) // 2],
        "trial_matrix_sha256": hashlib.sha256(canonical.encode()).hexdigest(),
        "trials": trials,
        "splits": splits,
    }
