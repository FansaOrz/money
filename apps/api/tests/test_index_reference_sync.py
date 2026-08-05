"""官方指数当前权重、成员事件和行业 PIT 测试。"""

from datetime import date
from io import BytesIO

import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    IndexConstituent,
    IndexMembershipEvent,
    QuantDataRecord,
    StockIndustry,
)
from app.services import index_reference_sync as service


class Response:
    def __init__(self, content: bytes) -> None:
        self.content = content

    def raise_for_status(self) -> None:
        return None


def _workbook(
    index_code: str,
    rows: list[tuple[str, str, float]],
    *,
    day: str = "20260805",
) -> bytes:
    frame = pd.DataFrame(
        [
            [
                day,
                index_code,
                "指数",
                "Index",
                code,
                name,
                name,
                "SSE",
                "SSE",
                weight,
            ]
            for code, name, weight in rows
        ]
    )
    stream = BytesIO()
    frame.to_excel(stream, index=False)
    return stream.getvalue()


def test_official_weight_switch_is_atomic_and_creates_events(
    db_session: Session, tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(
        service, "EXPECTED_COUNTS", {"000300": 2, "000905": 2}
    )
    db_session.add_all(
        [
            IndexConstituent(
                index_code="000300", stock_code="600001", stock_name="旧一"
            ),
            IndexConstituent(
                index_code="000905", stock_code="000001", stock_name="旧二"
            ),
        ]
    )
    for code in ("600001", "600002", "000001", "000002"):
        db_session.add(
            StockIndustry(
                code=code,
                source="stocktoday_sw2021",
                industry_code="801010",
                industry_name="农林牧渔",
            )
        )
    db_session.commit()
    files = {
        "000300": _workbook(
            "000300",
            [("600001", "一", 60.0), ("600002", "二", 40.0)],
        ),
        "000905": _workbook(
            "000905",
            [("000001", "三", 55.0), ("000002", "四", 45.0)],
        ),
    }

    def get(url: str, timeout: int) -> Response:
        assert timeout == 60
        code = "000300" if "000300" in url else "000905"
        return Response(files[code])

    result = service.sync_official_index_weights(
        db_session, request_get=get, data_root=tmp_path
    )
    assert result["weights_inserted"] == 4
    assert result["snapshot_dates"] == {
        "000300": "2026-08-05",
        "000905": "2026-08-05",
    }
    assert db_session.query(IndexConstituent).count() == 4
    additions = db_session.scalars(
        select(IndexMembershipEvent).where(
            IndexMembershipEvent.event_type == "add"
        )
    ).all()
    assert {row.stock_code for row in additions} == {"600002", "000002"}
    assert db_session.query(QuantDataRecord).filter_by(
        dataset="index_weight"
    ).count() == 4
    industry = service.capture_industry_pit(
        db_session, observed_on=date(2026, 8, 5)
    )
    assert industry["coverage"] == 4
    assert db_session.query(QuantDataRecord).filter_by(
        dataset="industry_classification"
    ).count() == 4
    assert all(item["ready"] for item in service.source_health(db_session).values())


def test_invalid_second_index_does_not_replace_current_members(
    db_session: Session, tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(
        service, "EXPECTED_COUNTS", {"000300": 2, "000905": 2}
    )
    db_session.add(
        IndexConstituent(
            index_code="000300", stock_code="600099", stock_name="保留"
        )
    )
    db_session.commit()
    good = _workbook(
        "000300", [("600001", "一", 60.0), ("600002", "二", 40.0)]
    )
    bad = _workbook(
        "000905", [("000001", "三", 20.0), ("000002", "四", 20.0)]
    )

    def get(url: str, timeout: int) -> Response:
        return Response(good if "000300" in url else bad)

    try:
        service.sync_official_index_weights(
            db_session, request_get=get, data_root=tmp_path
        )
    except ValueError as exc:
        assert "权重和" in str(exc)
    else:
        raise AssertionError("非法权重必须失败")
    members = db_session.scalars(select(IndexConstituent.stock_code)).all()
    assert members == ["600099"]
