"""CSCV/PBO 对随机无效策略膨胀的响应测试。"""

import random

from app.services.backtest_overfitting import cscv_pbo


def test_more_random_strategies_raise_overfitting_risk() -> None:
    stable = {"stable": [0.2] * 8}
    base = cscv_pbo(stable)
    generator = random.Random(20260805)
    expanded = dict(stable)
    for index in range(50):
        expanded[f"noise-{index:02d}"] = [
            generator.uniform(-1.0, 1.0) for _ in range(8)
        ]
    noisy = cscv_pbo(expanded)
    assert base["probability_backtest_overfitting"] == 0.0
    assert noisy["probability_backtest_overfitting"] > 0.0
    assert noisy["trial_matrix_sha256"]
    assert noisy["split_count"] > 1
