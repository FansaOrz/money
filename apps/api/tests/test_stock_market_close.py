"""A股收盘快照快速通道与多源日线归一化。"""

from datetime import date, datetime
from pathlib import Path

import pandas as pd
from sqlalchemy.orm import Session

from app.models import IndexConstituent, StockDailyBar, StockMaster
from app.services.research import ak_fetch, parquet_store, stock_data
from app.timezone import CN_TZ


def test_eastmoney_daily_parser_converts_lots_and_percent() -> None:
    frame = pd.DataFrame(
        [
            {
                "日期": "2026-08-04",
                "股票代码": "600000",
                "开盘": 10.0,
                "收盘": 10.2,
                "最高": 10.3,
                "最低": 9.9,
                "成交量": 1234,
                "成交额": 1_234_000,
                "换手率": 1.5,
                "涨跌幅": 2.0,
            }
        ]
    )
    parsed = stock_data.parse_eastmoney_daily_frame("600000", frame)
    row = parsed.iloc[0]
    assert row["volume"] == 123_400
    assert row["pct_change"] == 0.02
    assert row["trade_date"] == date(2026, 8, 4)


def test_tencent_daily_parser_corrects_volume_scale() -> None:
    frame = pd.DataFrame(
        [
            {
                "date": "2026-08-03",
                "open": 44.69,
                "close": 43.5,
                "high": 44.75,
                "low": 42.9,
                "volume": 1_047_315_600,
                "turnover": 0.0186,
                "amount": 457_984_200,
            }
        ]
    )
    row = stock_data.parse_tencent_daily_frame("689009", frame).iloc[0]
    assert row["volume"] == 10_473_156
    assert row["trade_date"] == date(2026, 8, 3)


def test_market_close_snapshot_updates_tracked_universe(
    db_session: Session, tmp_path: Path, monkeypatch
) -> None:
    db_session.add(StockMaster(code="600000", name="浦发银行", exchange="sh"))
    db_session.add(
        IndexConstituent(
            index_code="000300", stock_code="600000", stock_name="浦发银行"
        )
    )
    db_session.commit()
    monkeypatch.setattr(
        ak_fetch,
        "fetch_stock_spot_eastmoney",
        lambda: pd.DataFrame(
            [
                {
                    "代码": "600000",
                    "最新价": 10.2,
                    "今开": 10.0,
                    "最高": 10.3,
                    "最低": 9.9,
                    "成交量": 1234,
                    "成交额": 1_234_000,
                    "涨跌幅": 2.0,
                    "换手率": 1.5,
                },
                {
                    "代码": "000001",  # 不在冻结指数范围，不写入
                    "最新价": 12.0,
                },
            ]
        ),
    )
    day = date(2026, 8, 4)
    result = stock_data.sync_stock_market_close(
        db_session, trade_date=day, root=tmp_path
    )
    assert result["status"] == "success"
    assert result["updated"] == 1
    meta = db_session.get(StockDailyBar, "600000")
    assert meta is not None
    assert meta.last_trade_date == day
    assert meta.source == "eastmoney_spot"
    stored = parquet_store.read_daily("600000", root=tmp_path)
    assert stored is not None
    assert stored.iloc[-1]["volume"] == 123_400
    assert stored.iloc[-1]["pct_change"] == 0.02


def test_market_close_snapshot_skips_non_trading_day(
    db_session: Session, monkeypatch
) -> None:
    called = False

    def spot() -> pd.DataFrame:
        nonlocal called
        called = True
        return pd.DataFrame([{"代码": "600000", "最新价": 10.2}])

    monkeypatch.setattr(
        ak_fetch,
        "fetch_trade_calendar_sina",
        lambda: pd.DataFrame({"trade_date": [date(2026, 8, 7)]}),
    )
    monkeypatch.setattr(ak_fetch, "fetch_stock_spot_eastmoney", spot)
    monkeypatch.setattr(
        stock_data,
        "now_cn",
        lambda: datetime(2026, 8, 8, 17, 5, tzinfo=CN_TZ),
    )

    result = stock_data.sync_stock_market_close(db_session)

    assert result["status"] == "paused"
    assert result["updated"] == 0
    assert called is False


def test_market_close_snapshot_falls_back_to_sina(
    db_session: Session, tmp_path: Path, monkeypatch
) -> None:
    db_session.add(StockMaster(code="600000", name="浦发银行", exchange="sh"))
    db_session.add(
        IndexConstituent(
            index_code="000300", stock_code="600000", stock_name="浦发银行"
        )
    )
    db_session.commit()
    monkeypatch.setattr(ak_fetch, "fetch_stock_spot_eastmoney", lambda: None)
    monkeypatch.setattr(
        ak_fetch,
        "fetch_stock_spot_sina",
        lambda: pd.DataFrame(
            [
                {
                    "代码": "sh600000",
                    "最新价": 10.2,
                    "今开": 10.0,
                    "最高": 10.3,
                    "最低": 9.9,
                    "成交量": 123_456,
                    "成交额": 1_234_000,
                    "涨跌幅": 2.0,
                }
            ]
        ),
    )

    result = stock_data.sync_stock_market_close(
        db_session, trade_date=date(2026, 8, 4), root=tmp_path
    )

    assert result["status"] == "success"
    meta = db_session.get(StockDailyBar, "600000")
    assert meta is not None and meta.source == "sina_spot"
    stored = parquet_store.read_daily("600000", root=tmp_path)
    assert stored is not None
    assert stored.iloc[-1]["volume"] == 123_456
