"""主要市场指数服务与接口测试。

覆盖：
- AKShare 新浪系 DataFrame 归一化（date/OHLC/volume/change_pct）；
- 指数清单初始化（6 个指数）；
- 按 (index_id, trade_date) 幂等 upsert；
- change_pct 与前一交易日衔接的重算；
- 数据源失败时的优雅降级；
- 路由注册与响应结构（摘要 / 历史 / 未知代码 404）。
"""

from datetime import date
from decimal import Decimal

import pandas as pd
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models import IndexQuote, MarketIndex
from app.services import index_data
from app.services.index_data import (
    INDEX_DEFINITIONS,
    ensure_indices,
    parse_index_frame,
    sync_index_history,
)


def _frame(rows: list[tuple[str, float, float, float, float, float]]) -> pd.DataFrame:
    """构造与 stock_zh_index_daily 同构的 DataFrame。"""
    return pd.DataFrame(
        rows, columns=["date", "open", "high", "low", "close", "volume"]
    )


SAMPLE_FRAME = _frame(
    [
        ("2026-07-24", 3600.0, 3620.0, 3590.0, 3610.0, 3_0000_0000),
        ("2026-07-27", 3612.0, 3640.0, 3605.0, 3636.0, 3_2000_0000),
        ("2026-07-28", 3630.0, 3635.0, 3600.0, 3608.0, 2_8000_0000),
    ]
)


class TestParseFrame:
    def test_parse_normalizes_ohlc_and_change_pct(self) -> None:
        rows = parse_index_frame(SAMPLE_FRAME)
        assert len(rows) == 3
        first, second, third = rows
        assert first["trade_date"] == date(2026, 7, 24)
        assert first["close"] == Decimal("3610")
        assert first["volume"] == 3_0000_0000
        # 首行没有前收盘价，change_pct 为 None
        assert first["change_pct"] is None
        # 3636 / 3610 - 1 ≈ 0.7202%
        assert second["change_pct"] == Decimal("0.7202")
        # 3608 / 3636 - 1 ≈ -0.7701%
        assert third["change_pct"] == Decimal("-0.7701")

    def test_parse_skips_dirty_rows(self) -> None:
        frame = _frame(
            [
                ("2026-07-24", 1.0, 1.0, 1.0, 100.0, 100),
                ("not-a-date", 1.0, 1.0, 1.0, 101.0, 100),
                ("2026-07-27", 1.0, 1.0, 1.0, float("nan"), 100),
            ]
        )
        rows = parse_index_frame(frame)
        assert len(rows) == 1
        assert rows[0]["trade_date"] == date(2026, 7, 24)

    def test_parse_dedupes_same_date_keeps_last(self) -> None:
        frame = _frame(
            [
                ("2026-07-24", 1.0, 1.0, 1.0, 100.0, 100),
                ("2026-07-24", 1.0, 1.0, 1.0, 101.0, 200),
            ]
        )
        rows = parse_index_frame(frame)
        assert len(rows) == 1
        assert rows[0]["close"] == Decimal("101")

    def test_parse_days_tail_window(self) -> None:
        rows = parse_index_frame(SAMPLE_FRAME, days=2)
        assert [r["trade_date"] for r in rows] == [date(2026, 7, 27), date(2026, 7, 28)]


class TestEnsureIndices:
    def test_creates_all_six_indices(self, db_session: Session) -> None:
        indices = ensure_indices(db_session)
        assert len(indices) == 6
        codes = {item.code for item in indices}
        assert codes == {"SH000001", "CSI300", "HSI", "HSTECH", "SPX", "IXIC"}
        by_code = {item.code: item for item in indices}
        assert by_code["SH000001"].source_symbol == "sh000001"
        assert by_code["CSI300"].source_symbol == "sh000300"
        assert by_code["HSI"].market == "hk"
        assert by_code["SPX"].source_symbol == ".INX"
        assert by_code["IXIC"].currency == "USD"
        # 幂等：再次调用不产生重复
        ensure_indices(db_session)
        assert db_session.query(MarketIndex).count() == 6


def _fake_fetch(self_code_rows: dict[str, pd.DataFrame]):
    """构造 fetch_index_quotes 的替身：按指数代码返回固定 DataFrame。"""

    def fetch(code: str, days: int | None = None) -> list[dict]:
        frame = self_code_rows.get(code)
        if frame is None:
            return []
        return parse_index_frame(frame, days=days)

    return fetch


class TestSync:
    def test_sync_upserts_idempotently(
        self, db_session: Session, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        frames = {d["code"]: SAMPLE_FRAME for d in INDEX_DEFINITIONS}
        monkeypatch.setattr(index_data, "fetch_index_quotes", _fake_fetch(frames))

        result = sync_index_history(db_session, days=30)
        assert result["total_indices"] == 6
        assert result["updated_indices"] == 6
        assert result["failed"] == 0
        assert result["rows"] == 18
        assert db_session.query(IndexQuote).count() == 18

        # 再次同步同一批数据：行数不翻倍（幂等）
        result2 = sync_index_history(db_session, days=30)
        assert result2["rows"] == 18
        assert db_session.query(IndexQuote).count() == 18

    def test_sync_marks_failed_when_source_empty(
        self, db_session: Session, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        frames = {"SH000001": SAMPLE_FRAME}  # 其余指数无数据
        monkeypatch.setattr(index_data, "fetch_index_quotes", _fake_fetch(frames))
        result = sync_index_history(db_session, days=30)
        assert result["updated_indices"] == 1
        assert result["failed"] == 5
        assert len(result["errors"]) == 5
        assert db_session.query(IndexQuote).count() == 3

    def test_change_pct_stitches_with_previous_trading_day(
        self, db_session: Session, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """增量同步时，新区间首行 change_pct 应基于库中前一交易日收盘价。"""
        monkeypatch.setattr(
            index_data,
            "fetch_index_quotes",
            _fake_fetch({"SH000001": SAMPLE_FRAME}),
        )
        # 首次只同步前两天
        frame_head = _frame(
            [
                ("2026-07-24", 1.0, 1.0, 1.0, 3610.0, 100),
                ("2026-07-27", 1.0, 1.0, 1.0, 3636.0, 100),
            ]
        )
        monkeypatch.setattr(
            index_data, "fetch_index_quotes", _fake_fetch({"SH000001": frame_head})
        )
        sync_index_history(db_session, days=30)
        # 第二次仅抓到 7-28 的新数据（模拟仅增量抓取窗口）
        frame_tail = _frame([("2026-07-28", 1.0, 1.0, 1.0, 3608.0, 100)])
        monkeypatch.setattr(
            index_data, "fetch_index_quotes", _fake_fetch({"SH000001": frame_tail})
        )
        sync_index_history(db_session, days=30)

        quote = db_session.query(IndexQuote).filter_by(trade_date=date(2026, 7, 28)).one()
        # 应基于 7-27 收盘 3636 计算：3608/3636-1 ≈ -0.7701%
        # （SQLite 无原生 Decimal，经浮点往返，故用容差比较）
        assert quote.change_pct is not None
        assert abs(Decimal(str(quote.change_pct)) - Decimal("-0.7701")) < Decimal("0.0001")


class TestRoutes:
    def _seed(self, db: Session, monkeypatch: pytest.MonkeyPatch) -> None:
        frames = {d["code"]: SAMPLE_FRAME for d in INDEX_DEFINITIONS}
        monkeypatch.setattr(index_data, "fetch_index_quotes", _fake_fetch(frames))
        sync_index_history(db, days=30)

    def test_list_indices_summary(
        self, client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._seed(db_session, monkeypatch)
        response = client.get("/api/indices")
        assert response.status_code == 200
        body = response.json()
        assert body["total"] == 6
        assert len(body["items"]) == 6
        sh = next(item for item in body["items"] if item["code"] == "SH000001")
        assert sh["name"] == "上证指数"
        assert sh["latest_date"] == "2026-07-28"
        assert abs(Decimal(sh["close"]) - Decimal("3608")) < Decimal("0.01")
        assert abs(Decimal(sh["change_pct"]) - Decimal("-0.7701")) < Decimal("0.0001")
        spx = next(item for item in body["items"] if item["code"] == "SPX")
        assert spx["market"] == "us"

    def test_index_history_days(
        self, client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._seed(db_session, monkeypatch)
        response = client.get("/api/indices/SH000001/history?days=2")
        assert response.status_code == 200
        body = response.json()
        assert body["code"] == "SH000001"
        assert body["days"] == 2
        assert len(body["items"]) == 2
        # 升序返回
        assert body["items"][0]["date"] == "2026-07-27"
        assert body["items"][1]["date"] == "2026-07-28"
        assert abs(Decimal(body["items"][1]["change_pct"]) - Decimal("-0.7701")) < Decimal(
            "0.0001"
        )

    def test_history_case_insensitive_code(
        self, client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._seed(db_session, monkeypatch)
        response = client.get("/api/indices/sh000001/history?days=5")
        assert response.status_code == 200
        assert response.json()["code"] == "SH000001"

    def test_history_unknown_code_404(self, client: TestClient) -> None:
        response = client.get("/api/indices/UNKNOWN/history")
        assert response.status_code == 404

    def test_history_rejects_invalid_days(self, client: TestClient) -> None:
        response = client.get("/api/indices/SH000001/history?days=0")
        assert response.status_code == 422

    def test_sync_route(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        frames = {d["code"]: SAMPLE_FRAME for d in INDEX_DEFINITIONS}
        monkeypatch.setattr(index_data, "fetch_index_quotes", _fake_fetch(frames))
        response = client.post("/api/indices/sync")
        assert response.status_code == 200
        body = response.json()
        assert body["updated_indices"] == 6
        assert body["failed"] == 0
        assert body["rows"] == 18
