"""简单可解释、严格使用历史数据的组合风险覆盖层（研究态）。"""

from __future__ import annotations

import math
from collections.abc import Sequence

import numpy as np


def simple_risk_overlay(
    historical_returns: Sequence[float],
    *,
    current_drawdown: float,
    target_annual_volatility: float = 0.15,
    trend_window: int = 120,
    switching_cost: float = 0.001,
) -> dict[str, float | str]:
    if len(historical_returns) < max(20, trend_window):
        return {"status": "insufficient_history", "exposure": 0.0, "cost": 0.0}
    recent = np.asarray(historical_returns[-20:], dtype=float)
    realized = float(np.std(recent, ddof=1) * math.sqrt(252))
    vol_scale = min(1.0, target_annual_volatility / max(realized, 1e-8))
    trend_growth = float(np.prod(1 + np.asarray(historical_returns[-trend_window:])))
    trend_scale = 1.0 if trend_growth >= 1 else 0.5
    drawdown_scale = 1.0 if current_drawdown > -0.10 else 0.5 if current_drawdown > -0.20 else 0.0
    exposure = vol_scale * trend_scale * drawdown_scale
    return {
        "status": "challenger",
        "exposure": exposure,
        "realized_volatility": realized,
        "trend_scale": trend_scale,
        "drawdown_scale": drawdown_scale,
        "cost": abs(1 - exposure) * switching_cost,
    }
