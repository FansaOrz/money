"""Ridge/Elastic-Net challenger 的嵌套时间选择和反未来测试。"""

from datetime import date

from app.services.linear_alpha_challenger import (
    AlphaRow,
    FEATURES,
    walk_forward_linear_challenger,
)


def _rows() -> list[AlphaRow]:
    rows = []
    for month in range(1, 19):
        year = 2024 + (month - 1) // 12
        calendar_month = (month - 1) % 12 + 1
        day = date(year, calendar_month, 1)
        for index in range(30):
            features = {
                feature: (index - 15) / 10 + offset * 0.01
                for offset, feature in enumerate(FEATURES)
            }
            target = 0.02 * features["quality"] + 0.01 * features["value"]
            rows.append(
                AlphaRow(
                    signal_date=day,
                    code=f"{index:06d}",
                    industry="A" if index % 2 else "B",
                    features=features,
                    forward_return=target,
                    baseline_score=sum(features.values()),
                )
            )
    return rows


def test_walk_forward_predictions_and_coefficients_are_oos() -> None:
    rows = _rows()
    first = walk_forward_linear_challenger(rows)
    assert first["status"] == "challenger_only"
    assert first["oos_periods"] > 0
    assert first["coefficient_history"]
    earliest = first["coefficient_history"][0]
    assert earliest["training_end"] < earliest["prediction_date"]
    # 修改最后一期标签不能改变更早预测。
    mutated = [
        AlphaRow(
            **{
                **row.__dict__,
                "forward_return": (
                    row.forward_return * 100
                    if row.signal_date == max(item.signal_date for item in rows)
                    else row.forward_return
                ),
            }
        )
        for row in rows
    ]
    second = walk_forward_linear_challenger(mutated)
    first_early = [
        item for item in first["predictions"] if item["signal_date"] != "2025-06-01"
    ]
    second_early = [
        item for item in second["predictions"] if item["signal_date"] != "2025-06-01"
    ]
    assert first_early == second_early
