"""逐期分档与随机打分不能轻易通过单调门禁。"""

import random
from datetime import date

from app.services.quintile_evidence import quintile_evidence


def test_perfect_signal_passes_after_cost_but_random_signal_fails() -> None:
    perfect_scores = []
    random_scores = []
    forwards = []
    generator = random.Random(20260805)
    for month in range(1, 25):
        day = date(2023 + (month - 1) // 12, (month - 1) % 12 + 1, 1)
        returns = {f"{index:06d}": index / 10_000 for index in range(100)}
        perfect_scores.append((day, {code: value for code, value in returns.items()}))
        shuffled = list(returns.values())
        generator.shuffle(shuffled)
        random_scores.append(
            (day, dict(zip(returns, shuffled, strict=True)))
        )
        forwards.append((day, returns))
    perfect = quintile_evidence(
        perfect_scores,
        forwards,
        one_way_cost_rate=0.0,
        minimum_economic_spread=0.001,
    )
    random_result = quintile_evidence(
        random_scores,
        forwards,
        one_way_cost_rate=0.0,
        minimum_economic_spread=0.001,
    )
    assert perfect["passed"] is True
    assert random_result["passed"] is False
