"""A 股 T+1、FIFO 成本与公司行为共用的持仓批次账本。"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date, timedelta
import hashlib
from typing import Iterable
from uuid import uuid4


@dataclass
class PositionLot:
    lot_id: str
    acquired_date: date
    sellable_date: date
    shares: float
    total_cost: float
    source: str


def next_calendar_settlement_day(day: date) -> date:
    """OMS 无交易日历时的保守 T+1 日期；周末不会产生交易撮合。"""
    return day + timedelta(days=1)


class LotLedger:
    def __init__(self, lots: dict[str, list[PositionLot]] | None = None) -> None:
        self._lots = lots or {}

    @classmethod
    def from_payload(
        cls, payload: dict[str, list[dict[str, object]]] | None
    ) -> "LotLedger":
        lots: dict[str, list[PositionLot]] = {}
        for code, rows in dict(payload or {}).items():
            lots[code] = [
                PositionLot(
                    lot_id=str(
                        row.get("lot_id")
                        or hashlib.sha256(
                            "|".join(
                                (
                                    code,
                                    str(row["acquired_date"]),
                                    str(row["sellable_date"]),
                                    str(row["shares"]),
                                    str(row.get("source") or "unknown"),
                                )
                            ).encode()
                        ).hexdigest()[:24]
                    ),
                    acquired_date=date.fromisoformat(str(row["acquired_date"])),
                    sellable_date=date.fromisoformat(str(row["sellable_date"])),
                    shares=float(row["shares"]),
                    total_cost=float(row["total_cost"]),
                    source=str(row.get("source") or "unknown"),
                )
                for row in rows
            ]
        return cls(lots)

    def to_payload(self) -> dict[str, list[dict[str, object]]]:
        return {
            code: [
                {
                    **asdict(lot),
                    "acquired_date": lot.acquired_date.isoformat(),
                    "sellable_date": lot.sellable_date.isoformat(),
                }
                for lot in rows
                if lot.shares > 1e-9
            ]
            for code, rows in self._lots.items()
            if any(lot.shares > 1e-9 for lot in rows)
        }

    def lots(self, code: str) -> tuple[PositionLot, ...]:
        return tuple(self._lots.get(code, ()))

    def total(self, code: str) -> float:
        return sum(lot.shares for lot in self._lots.get(code, ()))

    def available(self, code: str, as_of: date) -> float:
        return sum(
            lot.shares for lot in self._lots.get(code, ()) if lot.sellable_date <= as_of
        )

    def buy(
        self,
        code: str,
        shares: float,
        total_cost: float,
        *,
        acquired_date: date,
        sellable_date: date,
        source: str,
    ) -> None:
        if shares <= 0 or total_cost < 0 or sellable_date <= acquired_date:
            raise ValueError("买入批次的股份、成本或 T+1 可卖日期无效")
        self._lots.setdefault(code, []).append(
            PositionLot(
                lot_id=uuid4().hex,
                acquired_date=acquired_date,
                sellable_date=sellable_date,
                shares=float(shares),
                total_cost=float(total_cost),
                source=source,
            )
        )

    def sell(
        self,
        code: str,
        shares: float,
        *,
        trade_date: date,
    ) -> list[dict[str, object]]:
        if shares <= 0:
            raise ValueError("卖出数量必须为正")
        if shares > self.available(code, trade_date) + 1e-9:
            raise ValueError("卖出数量超过 T+1 已结算可卖股份")
        remaining = float(shares)
        consumed: list[dict[str, object]] = []
        for lot in sorted(
            self._lots.get(code, ()),
            key=lambda item: (item.acquired_date, item.sellable_date),
        ):
            if lot.sellable_date > trade_date or remaining <= 1e-9:
                continue
            take = min(lot.shares, remaining)
            cost = lot.total_cost * take / lot.shares if lot.shares > 0 else 0.0
            lot.shares -= take
            lot.total_cost -= cost
            remaining -= take
            consumed.append(
                {
                    "lot_id": lot.lot_id,
                    "acquired_date": lot.acquired_date.isoformat(),
                    "sellable_date": lot.sellable_date.isoformat(),
                    "shares": take,
                    "cost": cost,
                    "source": lot.source,
                }
            )
        self._lots[code] = [
            lot for lot in self._lots.get(code, ()) if lot.shares > 1e-9
        ]
        if not self._lots[code]:
            self._lots.pop(code, None)
        return consumed

    def distribute_shares(
        self,
        code: str,
        ratio: float,
        *,
        action_date: date,
        source: str,
    ) -> None:
        if ratio <= 0:
            return
        for lot in self._lots.get(code, ()):
            lot.shares *= 1.0 + ratio
            lot.source = f"{lot.source}|share_distribution:{action_date}:{source}"

    def remove(self, code: str) -> tuple[PositionLot, ...]:
        return tuple(self._lots.pop(code, ()))

    def replace_lots(self, code: str, lots: Iterable[PositionLot]) -> None:
        self._lots[code] = list(lots)

    def scale_total(self, code: str, target_shares: float) -> None:
        """按比例把批次总股数调整到登记股数，成本保持不变。"""
        current = self.total(code)
        if target_shares < 0 or current <= 0:
            if target_shares == 0:
                self._lots.pop(code, None)
                return
            raise ValueError("持仓批次总股数无法缩放")
        ratio = target_shares / current
        for lot in self._lots.get(code, ()):
            lot.shares *= ratio
