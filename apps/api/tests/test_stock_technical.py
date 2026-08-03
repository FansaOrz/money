from datetime import date, timedelta

from app.services.stock_technical import analyze_technical


def _rows(count: int = 80, step: float = 0.5) -> list[dict]:
    start = date(2025, 1, 1)
    return [
        {
            "trade_date": start + timedelta(days=index),
            "open": 10 + index * step - 0.1,
            "high": 10 + index * step + 0.3,
            "low": 10 + index * step - 0.3,
            "close": 10 + index * step,
            "volume": 1_000_000 + index * 1000,
        }
        for index in range(count)
    ]


def test_analyze_technical_detects_uptrend() -> None:
    result = analyze_technical("600000", _rows())

    assert result["sufficient"] is True
    assert result["trend"] in {"bullish", "strong_bullish"}
    assert result["score"] > 0
    assert result["indicators"]["ma5"] > result["indicators"]["ma20"]
    assert result["indicators"]["macd_dif"] > result["indicators"]["macd_dea"]
    assert result["methodology"]


def test_analyze_technical_reports_insufficient_history() -> None:
    result = analyze_technical("600000", _rows(12))

    assert result["sufficient"] is False
    assert result["trend"] == "insufficient"
    assert result["sample_size"] == 12
    assert result["risks"]


def test_analyze_technical_never_uses_invalid_close() -> None:
    rows = _rows(40)
    rows.extend(
        [
            {"trade_date": date(2026, 1, 1), "close": None},
            {"trade_date": date(2026, 1, 2), "close": 0},
        ]
    )
    result = analyze_technical("600000", rows)

    assert result["sample_size"] == 40
    assert result["as_of"] == rows[39]["trade_date"]
