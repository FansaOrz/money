"""输入字段最早可用时间契约和 T 时点泄漏门禁。"""

from __future__ import annotations

from datetime import datetime


FIELD_AVAILABILITY = {
    "daily.open": "market_open",
    "daily.close": "market_close",
    "daily.volume": "market_close",
    "financial.value": "announcement_time",
    "financial.restated_value": "restatement_announcement_time",
    "index_membership": "index_notice_effective_time",
    "corporate_action": "official_announcement_time",
}


def assert_point_in_time_inputs(
    rows: list[dict[str, object]], *, decision_at: datetime
) -> None:
    violations: list[str] = []
    for row in rows:
        available_at = row.get("available_at")
        if not isinstance(available_at, datetime):
            violations.append(f"{row.get('field')}:缺少 available_at")
        elif available_at > decision_at:
            violations.append(f"{row.get('field')}@{available_at.isoformat()}")
    if violations:
        raise ValueError("发现 T 后字段影响 T 时决策：" + ",".join(violations))


def history_unchanged_after_future_mutation(
    signal_builder,
    rows: list[dict[str, object]],
    *,
    decision_at: datetime,
) -> bool:
    baseline = signal_builder(rows, decision_at)
    mutated = [
        {
            **row,
            "value": (
                987654321
                if isinstance(row.get("available_at"), datetime)
                and row["available_at"] > decision_at
                else row.get("value")
            ),
        }
        for row in rows
    ]
    return signal_builder(mutated, decision_at) == baseline
