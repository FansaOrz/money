"""回测与前向共用的不可变事件流黄金账本回放器。"""

from __future__ import annotations

from copy import deepcopy
from decimal import Decimal


D = Decimal


def replay(events: list[dict[str, object]], *, initial_cash: Decimal) -> list[dict]:
    cash = D(initial_cash)
    positions: dict[str, D] = {}
    marks: dict[str, D] = {}
    receivables: dict[str, D] = {}
    fees = D("0")
    snapshots: list[dict] = []
    applied: set[str] = set()
    for event in events:
        event_id = str(event["id"])
        if event_id in applied:
            continue
        applied.add(event_id)
        kind = str(event["type"])
        code = str(event.get("code", ""))
        quantity = D(str(event.get("quantity", 0)))
        price = D(str(event.get("price", 0)))
        fee = D(str(event.get("fee", 0)))
        if kind == "buy":
            cash -= quantity * price + fee
            positions[code] = positions.get(code, D("0")) + quantity
            marks[code] = price
            fees += fee
        elif kind == "sell":
            if positions.get(code, D("0")) < quantity:
                raise ValueError("卖出后持仓不得为负")
            cash += quantity * price - fee
            positions[code] -= quantity
            marks[code] = price
            fees += fee
        elif kind == "tax":
            amount = D(str(event["amount"]))
            cash -= amount
            fees += amount
        elif kind == "dividend_record":
            receivables[event_id] = positions.get(code, D("0")) * D(
                str(event["cash_per_share"])
            )
        elif kind == "dividend_pay":
            source = str(event["record_event_id"])
            cash += receivables.pop(source, D("0"))
        elif kind in {"split", "bonus"}:
            ratio = D(str(event["ratio"]))
            positions[code] = positions.get(code, D("0")) * ratio
            if code in marks:
                marks[code] /= ratio
        elif kind == "rights":
            allotted = positions.get(code, D("0")) * D(str(event["ratio"]))
            cost = allotted * price
            if cost <= cash:
                cash -= cost
                positions[code] = positions.get(code, D("0")) + allotted
        elif kind == "swap":
            target = str(event["target_code"])
            ratio = D(str(event["ratio"]))
            converted = positions.pop(code, D("0")) * ratio
            positions[target] = positions.get(target, D("0")) + converted
            if code in marks:
                marks[target] = marks.pop(code) / ratio
        elif kind in {"suspension", "limit_block", "delist_notice"}:
            pass
        else:
            raise ValueError(f"未知账本事件：{kind}")
        if cash < 0:
            raise ValueError("现金不得为负")
        snapshots.append(
            {
                "event_id": event_id,
                "cash": str(cash),
                "positions": {
                    key: str(value)
                    for key, value in sorted(positions.items())
                    if value
                },
                "receivables": {
                    key: str(value) for key, value in sorted(receivables.items())
                },
                "fees": str(fees),
                "net_asset_value": str(
                    cash
                    + sum(
                        quantity * marks.get(key, D("0"))
                        for key, quantity in positions.items()
                    )
                    + sum(receivables.values(), D("0"))
                ),
            }
        )
    return deepcopy(snapshots)


def replay_backtest(events: list[dict[str, object]], initial_cash: Decimal) -> list[dict]:
    return replay(events, initial_cash=initial_cash)


def replay_forward(events: list[dict[str, object]], initial_cash: Decimal) -> list[dict]:
    return replay(events, initial_cash=initial_cash)
