"""现金可用、冻结、应收和已结算口径的守恒账本。"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date


CASH_POLICY_VERSION = "CNY_CASH_FLAT_2PCT_ACT365_V1"
ANNUAL_CASH_RATE = 0.02


@dataclass(frozen=True)
class CashEvent:
    event_date: date
    event_type: str
    reference: str
    available_delta: float = 0.0
    frozen_delta: float = 0.0
    receivable_delta: float = 0.0
    settled_delta: float = 0.0
    interest: float = 0.0
    fee: float = 0.0


@dataclass
class CashLedger:
    """已结算现金是可用现金的子集，不计入现金等价物的第二份资产。"""

    available: float
    settled: float
    frozen: float = 0.0
    receivable: float = 0.0
    events: list[CashEvent] = field(default_factory=list)
    _opening_available: float = field(init=False)
    _opening_frozen: float = field(init=False)
    _opening_receivable: float = field(init=False)
    _opening_settled: float = field(init=False)

    def __post_init__(self) -> None:
        self._opening_available = self.available
        self._opening_frozen = self.frozen
        self._opening_receivable = self.receivable
        self._opening_settled = self.settled
        self._validate()

    @property
    def cash_equivalent(self) -> float:
        return self.available + self.frozen + self.receivable

    def _record(
        self,
        day: date,
        event_type: str,
        reference: str,
        *,
        available: float = 0.0,
        frozen: float = 0.0,
        receivable: float = 0.0,
        settled: float = 0.0,
        interest: float = 0.0,
        fee: float = 0.0,
    ) -> None:
        self.available += available
        self.frozen += frozen
        self.receivable += receivable
        self.settled += settled
        self.events.append(
            CashEvent(
                event_date=day,
                event_type=event_type,
                reference=reference,
                available_delta=available,
                frozen_delta=frozen,
                receivable_delta=receivable,
                settled_delta=settled,
                interest=interest,
                fee=fee,
            )
        )
        self._validate()

    def accrue_interest(
        self,
        day: date,
        *,
        calendar_days: int,
        annual_rate: float = ANNUAL_CASH_RATE,
        reference: str = CASH_POLICY_VERSION,
    ) -> float:
        if calendar_days <= 0 or self.settled <= 0:
            return 0.0
        interest = round(
            self.settled * annual_rate * calendar_days / 365.0,
            2,
        )
        self._record(
            day,
            "cash_interest",
            reference,
            available=interest,
            settled=interest,
            interest=interest,
        )
        return interest

    def recognize_receivable(self, day: date, amount: float, reference: str) -> None:
        if amount <= 0:
            return
        self._record(
            day,
            "receivable_recognized",
            reference,
            receivable=amount,
        )

    def settle_receivable(self, day: date, amount: float, reference: str) -> None:
        if amount <= 0:
            return
        self._record(
            day,
            "receivable_settled",
            reference,
            available=amount,
            receivable=-amount,
            settled=amount,
        )

    def receive_cash(
        self,
        day: date,
        amount: float,
        reference: str,
        *,
        settled: bool = True,
        event_type: str = "cash_received",
        fee: float = 0.0,
    ) -> None:
        if amount < 0:
            raise ValueError("现金流入不能为负")
        self._record(
            day,
            event_type,
            reference,
            available=amount,
            settled=amount if settled else 0.0,
            fee=fee,
        )

    def debit_cash(
        self,
        day: date,
        amount: float,
        reference: str,
        *,
        event_type: str = "cash_debited",
        fee: float = 0.0,
    ) -> None:
        if amount < 0 or amount > self.available + 1e-8:
            raise ValueError("现金扣款超过可用余额")
        settled_debit = min(self.settled, amount)
        self._record(
            day,
            event_type,
            reference,
            available=-amount,
            settled=-settled_debit,
            fee=fee,
        )

    def reserve(self, day: date, amount: float, reference: str) -> None:
        if amount < 0 or amount > self.available + 1e-8:
            raise ValueError("冻结金额超过可用余额")
        self._record(
            day,
            "buy_order_frozen",
            reference,
            available=-amount,
            frozen=amount,
        )

    def consume_reservation(
        self,
        day: date,
        amount: float,
        reference: str,
        *,
        fee: float = 0.0,
    ) -> None:
        if amount < 0 or amount > self.frozen + 1e-8:
            raise ValueError("成交扣款超过冻结余额")
        settled_debit = min(self.settled, amount)
        self._record(
            day,
            "buy_order_settled",
            reference,
            frozen=-amount,
            settled=-settled_debit,
            fee=fee,
        )

    def release_reservation(self, day: date, amount: float, reference: str) -> None:
        if amount < 0 or amount > self.frozen + 1e-8:
            raise ValueError("解冻金额超过冻结余额")
        self._record(
            day,
            "buy_order_released",
            reference,
            available=amount,
            frozen=-amount,
        )

    def settle_sale_proceeds(self, day: date, amount: float, reference: str) -> None:
        """卖出款 T 日已可交易，只在 T+1 增加可取/已结算子余额。"""
        if amount <= 0:
            return
        unsettled_available = max(self.available - self.settled, 0.0)
        settled_amount = min(amount, unsettled_available)
        self._record(
            day,
            "sale_proceeds_settled",
            reference,
            settled=settled_amount,
        )

    def conservation(self) -> dict[str, object]:
        available_delta = sum(item.available_delta for item in self.events)
        frozen_delta = sum(item.frozen_delta for item in self.events)
        receivable_delta = sum(item.receivable_delta for item in self.events)
        settled_delta = sum(item.settled_delta for item in self.events)
        expected = (
            self._opening_available
            + self._opening_frozen
            + self._opening_receivable
            + available_delta
            + frozen_delta
            + receivable_delta
        )
        error = self.cash_equivalent - expected
        return {
            "policy_version": CASH_POLICY_VERSION,
            "annual_rate": ANNUAL_CASH_RATE,
            "day_count": "ACT/365",
            "opening": {
                "available": self._opening_available,
                "frozen": self._opening_frozen,
                "receivable": self._opening_receivable,
                "settled": self._opening_settled,
            },
            "closing": {
                "available": self.available,
                "frozen": self.frozen,
                "receivable": self.receivable,
                "settled": self.settled,
            },
            "deltas": {
                "available": available_delta,
                "frozen": frozen_delta,
                "receivable": receivable_delta,
                "settled": settled_delta,
            },
            "cash_equivalent": self.cash_equivalent,
            "conservation_error": error,
            "events": [
                {
                    **asdict(item),
                    "event_date": item.event_date.isoformat(),
                }
                for item in self.events
            ],
        }

    def assert_conserved(self, tolerance: float = 1e-6) -> None:
        error = float(self.conservation()["conservation_error"])
        if abs(error) > tolerance:
            raise ValueError(f"现金账本不守恒，误差 {error:.8f}")
        self._validate()

    def _validate(self) -> None:
        for label, value in (
            ("available", self.available),
            ("settled", self.settled),
            ("frozen", self.frozen),
            ("receivable", self.receivable),
        ):
            if value < -1e-6:
                raise ValueError(f"{label} 现金余额为负：{value}")
        if self.settled > self.available + self.frozen + 1e-6:
            raise ValueError("已结算现金不能超过可用与冻结现金之和")
