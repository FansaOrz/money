"""主动收益和净 Alpha 统计门禁。"""

import random

from app.services.active_alpha_evidence import active_alpha_evidence


def test_positive_active_alpha_passes_and_negative_version_fails() -> None:
    generator = random.Random(20260805)
    market = [generator.gauss(0.0002, 0.01) for _ in range(756)]
    positive = [
        value + 0.001 + generator.gauss(0.0, 0.001)
        for value in market
    ]
    negative = [
        value - 0.001 + generator.gauss(0.0, 0.001)
        for value in market
    ]
    passed = active_alpha_evidence(positive, market, bootstrap_samples=500)
    failed = active_alpha_evidence(negative, market, bootstrap_samples=500)
    assert passed["passed"] is True
    assert passed["active_block_bootstrap_95_ci"][0] > 0
    assert passed["regression_alpha_block_bootstrap_95_ci"][0] > 0
    assert failed["passed"] is False
    assert failed["active_mean_daily"] < 0
    assert failed["regression_alpha_daily"] < 0
