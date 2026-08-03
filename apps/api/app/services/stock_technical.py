"""个股技术面摘要。

指标计算只使用按日期升序的 OHLCV 日线，不读取未来数据。该模块保持纯函数，
便于在 API、定时任务和测试中复用。
"""

from __future__ import annotations

from math import isfinite, sqrt
from typing import Any


def _number(value: object) -> float | None:
    try:
        result = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return result if isfinite(result) else None


def _ema(values: list[float], span: int) -> list[float]:
    alpha = 2.0 / (span + 1)
    output = [values[0]]
    for value in values[1:]:
        output.append(alpha * value + (1 - alpha) * output[-1])
    return output


def _rsi(values: list[float], period: int) -> float | None:
    if len(values) <= period:
        return None
    changes = [curr - prev for prev, curr in zip(values, values[1:])]
    gains = [max(change, 0.0) for change in changes]
    losses = [max(-change, 0.0) for change in changes]
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for gain, loss in zip(gains[period:], losses[period:]):
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period
    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 50.0
    return 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)


def _mean(values: list[float]) -> float:
    return sum(values) / len(values)


def _std(values: list[float]) -> float:
    mean = _mean(values)
    return sqrt(sum((value - mean) ** 2 for value in values) / len(values))


def analyze_technical(code: str, rows: list[dict[str, Any]]) -> dict[str, Any]:
    """计算技术指标和可解释结论；行情不足时返回显式的数据质量说明。"""
    bars: list[dict[str, Any]] = []
    for row in rows:
        close = _number(row.get("close"))
        if close is None or close <= 0 or row.get("trade_date") is None:
            continue
        bars.append(
            {
                "trade_date": row["trade_date"],
                "open": _number(row.get("open")) or close,
                "high": _number(row.get("high")) or close,
                "low": _number(row.get("low")) or close,
                "close": close,
                "volume": _number(row.get("volume")),
            }
        )
    bars.sort(key=lambda item: item["trade_date"])
    if len(bars) < 30:
        return {
            "code": code,
            "as_of": bars[-1]["trade_date"] if bars else None,
            "sufficient": False,
            "sample_size": len(bars),
            "trend": "insufficient",
            "score": 0,
            "summary": "历史数据不足，暂时无法判断趋势。",
            "indicators": {},
            "signals": [],
            "risks": [f"有效日线仅 {len(bars)} 条，至少需要 30 条"],
            "methodology": "所有指标仅使用 as_of 当日及之前的数据。",
        }

    closes = [bar["close"] for bar in bars]
    highs = [bar["high"] for bar in bars]
    lows = [bar["low"] for bar in bars]
    close = closes[-1]

    ma = {
        period: _mean(closes[-period:]) if len(closes) >= period else None
        for period in (5, 10, 20, 60)
    }
    ema12, ema26 = _ema(closes, 12), _ema(closes, 26)
    dif_series = [fast - slow for fast, slow in zip(ema12, ema26)]
    dea_series = _ema(dif_series, 9)
    dif, dea = dif_series[-1], dea_series[-1]
    macd = (dif - dea) * 2

    rsi6 = _rsi(closes, 6)
    rsi12 = _rsi(closes, 12)
    rsi24 = _rsi(closes, 24)

    k_value = 50.0
    d_value = 50.0
    for index in range(len(bars)):
        start = max(0, index - 8)
        highest = max(highs[start : index + 1])
        lowest = min(lows[start : index + 1])
        rsv = 50.0 if highest == lowest else (closes[index] - lowest) / (highest - lowest) * 100
        k_value = k_value * 2 / 3 + rsv / 3
        d_value = d_value * 2 / 3 + k_value / 3
    j_value = 3 * k_value - 2 * d_value

    middle = _mean(closes[-20:])
    deviation = _std(closes[-20:])
    boll_upper, boll_lower = middle + 2 * deviation, middle - 2 * deviation

    true_ranges: list[float] = []
    for index in range(1, len(bars)):
        true_ranges.append(
            max(
                highs[index] - lows[index],
                abs(highs[index] - closes[index - 1]),
                abs(lows[index] - closes[index - 1]),
            )
        )
    atr14 = _mean(true_ranges[-14:])
    atr_pct = atr14 / close
    support20, resistance20 = min(lows[-20:]), max(highs[-20:])

    recent_volumes = [bar["volume"] for bar in bars[-5:] if bar["volume"] is not None]
    base_volumes = [bar["volume"] for bar in bars[-25:-5] if bar["volume"] is not None]
    volume_ratio = (
        _mean(recent_volumes) / _mean(base_volumes)
        if recent_volumes and base_volumes and _mean(base_volumes) > 0
        else None
    )

    score = 0
    signals: list[str] = []
    risks: list[str] = []
    if ma[20] is not None:
        if close > ma[20]:
            score += 1
            signals.append("收盘价位于 MA20 上方，中期趋势偏强")
        else:
            score -= 1
            signals.append("收盘价位于 MA20 下方，中期趋势偏弱")
    if ma[5] is not None and ma[20] is not None:
        if ma[5] > ma[20]:
            score += 1
            signals.append("短期平均价格高于近一个月平均价格，上涨趋势正在延续")
        else:
            score -= 1
            signals.append("短期平均价格低于近一个月平均价格，走势仍偏弱")
    if dif > dea:
        score += 1
        signals.append("近期上涨动力强于下跌动力")
    else:
        score -= 1
        signals.append("近期下跌动力强于上涨动力")
    if rsi12 is not None:
        if 50 <= rsi12 <= 70:
            score += 1
            signals.append("近期表现相对强势，但还没有进入明显过热区间")
        elif rsi12 < 40:
            score -= 1
            signals.append("近期买入力量较弱，价格反弹尚未得到确认")
        if rsi12 > 70:
            risks.append("近期上涨较快，短期追高后出现回落的风险增加")
        elif rsi12 < 30:
            risks.append("近期下跌较快，即使出现反弹，价格波动也可能较大")
    if k_value > d_value:
        score += 1
        signals.append("最近几天的价格变化正在改善")
    else:
        score -= 1
        signals.append("最近几天的价格变化仍在转弱")
    if close >= boll_upper:
        risks.append("价格触及或突破布林带上轨，谨防短期均值回归")
    elif close <= boll_lower:
        risks.append("价格触及或跌破布林带下轨，弱势波动可能放大")
    if atr_pct >= 0.05:
        risks.append(f"ATR14 占价格 {atr_pct:.1%}，近期波动较高")
    if volume_ratio is not None and volume_ratio < 0.6:
        risks.append(f"近 5 日量能仅为此前 20 日的 {volume_ratio:.2f} 倍")

    trend = (
        "strong_bullish"
        if score >= 4
        else "bullish"
        if score >= 2
        else "strong_bearish"
        if score <= -4
        else "bearish"
        if score <= -2
        else "neutral"
    )
    summary = {
        "strong_bullish": "多个趋势指标同时转强，当前整体走势明显偏强。",
        "bullish": "上涨信号多于下跌信号，当前整体走势偏强。",
        "neutral": "上涨和下跌信号互相抵消，当前更接近震荡整理。",
        "bearish": "下跌信号多于上涨信号，当前整体走势偏弱。",
        "strong_bearish": "多个趋势指标同时转弱，当前整体走势明显偏弱。",
    }[trend]
    return {
        "code": code,
        "as_of": bars[-1]["trade_date"],
        "sufficient": True,
        "sample_size": len(bars),
        "trend": trend,
        "score": score,
        "summary": summary,
        "indicators": {
            "close": close,
            "ma5": ma[5],
            "ma10": ma[10],
            "ma20": ma[20],
            "ma60": ma[60],
            "macd_dif": dif,
            "macd_dea": dea,
            "macd_histogram": macd,
            "rsi6": rsi6,
            "rsi12": rsi12,
            "rsi24": rsi24,
            "kdj_k": k_value,
            "kdj_d": d_value,
            "kdj_j": j_value,
            "boll_upper": boll_upper,
            "boll_middle": middle,
            "boll_lower": boll_lower,
            "atr14": atr14,
            "atr_pct": atr_pct,
            "support20": support20,
            "resistance20": resistance20,
            "volume_ratio": volume_ratio,
        },
        "signals": signals,
        "risks": risks,
        "methodology": (
            "MA(5/10/20/60)、MACD(12/26/9)、Wilder RSI(6/12/24)、"
            "KDJ(9,3,3)、BOLL(20,2)、ATR14；只使用 as_of 当日及之前的前复权日线。"
        ),
    }


__all__ = ["analyze_technical"]
