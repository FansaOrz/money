"""仅作 challenger 的两状态概率过滤，不输出事后硬标签。"""

from __future__ import annotations

import math
from collections.abc import Sequence


def gaussian_state_probabilities(
    observations: Sequence[float],
    *,
    calm_sigma: float = 0.01,
    stress_sigma: float = 0.03,
    stay_probability: float = 0.95,
) -> list[dict[str, float]]:
    calm = stress = 0.5
    output: list[dict[str, float]] = []
    for value in observations:
        prior_calm = calm * stay_probability + stress * (1 - stay_probability)
        prior_stress = stress * stay_probability + calm * (1 - stay_probability)
        calm_likelihood = math.exp(-(value**2) / (2 * calm_sigma**2)) / calm_sigma
        stress_likelihood = math.exp(-(value**2) / (2 * stress_sigma**2)) / stress_sigma
        normalizer = (
            prior_calm * calm_likelihood + prior_stress * stress_likelihood
        )
        calm = prior_calm * calm_likelihood / max(normalizer, 1e-300)
        stress = 1 - calm
        output.append({"calm_probability": calm, "stress_probability": stress})
    return output
