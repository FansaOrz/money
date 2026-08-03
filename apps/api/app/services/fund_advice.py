"""把基金净值指标翻译为直白、可复核的趋势与仓位建议。"""

from __future__ import annotations

from typing import Any


def build_fund_advice(
    *,
    sample_count: int,
    trend_signal: str,
    return_20d: float | None,
    return_60d: float | None,
    annual_volatility: float | None,
    max_drawdown: float | None,
    sharpe: float | None,
) -> dict[str, Any]:
    """生成确定性规则建议，不调用大模型，不使用未来数据。"""
    reasons: list[str] = []
    risks: list[str] = []
    score = 50 + {
        "strong_up": 25, "up": 12, "neutral": 0, "down": -15, "strong_down": -28,
    }.get(trend_signal, 0)

    trend_text = {
        "strong_up": "短期和中期趋势同时向上",
        "up": "上涨信号多于下跌信号",
        "neutral": "上涨和下跌信号互相抵消，方向不明确",
        "down": "近期走势转弱，需要控制继续下跌的风险",
        "strong_down": "短期和中期趋势同时向下",
    }
    reasons.append(trend_text.get(trend_signal, "趋势暂不明确"))

    if return_60d is not None:
        if return_60d >= 0.08:
            score += 8
            reasons.append(f"近 3 个月上涨 {return_60d:.1%}，中期表现较强")
        elif return_60d > 0:
            score += 4
            reasons.append(f"近 3 个月仍有 {return_60d:.1%} 的正收益")
        elif return_60d <= -0.08:
            score -= 10
            risks.append(f"近 3 个月下跌 {abs(return_60d):.1%}，弱势尚未扭转")
        else:
            score -= 4
            reasons.append(f"近 3 个月收益为 {return_60d:.1%}，表现偏弱")

    overheated = return_20d is not None and return_20d >= 0.10
    if overheated:
        score -= 6
        risks.append(f"近 1 个月已上涨 {return_20d:.1%}，现在追高容易遇到回落")
    elif return_20d is not None and return_20d <= -0.08:
        score -= 5
        risks.append(f"近 1 个月下跌 {abs(return_20d):.1%}，暂未看到止跌确认")

    if sharpe is not None:
        if sharpe >= 1:
            score += 7
            reasons.append("过去一段时间承担的波动换来了较好的收益")
        elif sharpe < 0:
            score -= 7
            risks.append("过去一段时间的收益不足以补偿所承受的波动")
    if max_drawdown is not None:
        if max_drawdown <= -0.30:
            score -= 10
            risks.append(f"历史最大回撤达到 {abs(max_drawdown):.1%}，属于高回撤基金")
        elif max_drawdown <= -0.20:
            score -= 5
            risks.append(f"历史最大回撤约 {abs(max_drawdown):.1%}，仓位不宜过重")
    if annual_volatility is not None and annual_volatility >= 0.30:
        score -= 5
        risks.append(f"年化波动约 {annual_volatility:.1%}，净值上下起伏较大")

    confidence = "high" if sample_count >= 250 else "medium" if sample_count >= 120 else "low"
    score = max(0, min(100, round(score)))
    if confidence == "low":
        action, label = "watch", "暂时观望"
        summary = "历史净值样本较少，先不要根据这次结果调整仓位。"
        risks.insert(0, f"只有 {sample_count} 条净值记录，判断可靠性较低")
    elif score >= 72 and not overheated:
        action, label = "add", "可以考虑加仓"
        summary = "趋势和风险收益表现都偏正面，可以分批加仓，不建议一次性买满。"
    elif score >= 58:
        action, label = "hold", "继续持有"
        summary = (
            "基金整体仍可持有，但近期涨幅较大，暂时不建议追高。"
            if overheated else "当前没有明显的减仓信号，继续持有并观察趋势即可。"
        )
    elif score >= 42:
        action, label = "watch", "暂时观望"
        summary = "方向还不够明确，先维持现状，等趋势转强或转弱后再决定。"
    elif score >= 25:
        action, label = "reduce", "建议适当减仓"
        summary = "弱势信号较多，建议降低一部分仓位，避免继续下跌时损失扩大。"
    else:
        action, label = "reduce_more", "建议明显减仓"
        summary = "趋势和风险指标都偏弱，建议明显降低仓位，不要急着补仓摊低成本。"

    return {
        "action": action, "label": label, "score": score, "confidence": confidence,
        "horizon": "未来 1～3 个月", "summary": summary,
        "reasons": reasons[:4], "risks": risks[:4],
        "invalidation": "如果后续趋势方向发生反转，建议会随最新净值自动调整。",
    }


__all__ = ["build_fund_advice"]
