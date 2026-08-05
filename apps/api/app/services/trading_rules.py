"""按证券板块、日期和订单类型版本化的 A 股申报数量规则。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
import math


@dataclass(frozen=True)
class QuantityRule:
    version: str
    market: str
    board: str
    effective_from: date
    buy_minimum: int
    buy_increment: int
    sell_increment: int
    max_market_quantity: int
    max_limit_quantity: int
    odd_lot_sell_full_only: bool = True

    def maximum(self, order_type: str) -> int:
        return (
            self.max_limit_quantity
            if order_type == "limit"
            else self.max_market_quantity
        )

    def normalize_buy(self, quantity: float, order_type: str = "market") -> float:
        maximum = self.maximum(order_type)
        bounded = min(max(float(quantity), 0.0), float(maximum))
        normalized = (
            math.floor(bounded / self.buy_increment) * self.buy_increment
        )
        return float(normalized if normalized >= self.buy_minimum else 0)

    def normalize_sell(
        self,
        quantity: float,
        held: float,
        order_type: str = "market",
    ) -> float:
        bounded = min(
            max(float(quantity), 0.0),
            max(float(held), 0.0),
            float(self.maximum(order_type)),
        )
        if self.odd_lot_sell_full_only and abs(bounded - held) <= 1e-9:
            return float(held)
        return float(
            math.floor(bounded / self.sell_increment) * self.sell_increment
        )

    def validate(
        self,
        *,
        side: str,
        quantity: float,
        held: float = 0.0,
        order_type: str = "market",
    ) -> list[str]:
        reasons: list[str] = []
        if order_type not in {"market", "limit"}:
            reasons.append("订单类型必须为 market/limit")
            return reasons
        if quantity <= 0:
            reasons.append("申报数量必须为正")
            return reasons
        if quantity > self.maximum(order_type):
            reasons.append(
                f"申报数量超过 {self.version} 的{order_type}单上限 "
                f"{self.maximum(order_type)} 股"
            )
        if side == "buy":
            if quantity < self.buy_minimum:
                reasons.append(f"买入最低申报 {self.buy_minimum} 股")
            elif quantity % self.buy_increment != 0:
                reasons.append(f"买入必须按 {self.buy_increment} 股递增")
        elif side == "sell":
            if quantity > held + 1e-9:
                reasons.append("卖出数量超过可卖持仓")
            is_full_liquidation = abs(quantity - held) <= 1e-9
            if (
                quantity % self.sell_increment != 0
                and not (self.odd_lot_sell_full_only and is_full_liquidation)
            ):
                reasons.append(
                    f"零股只能一次性卖出全部，普通卖出按 "
                    f"{self.sell_increment} 股递增"
                )
        return reasons


_MAIN_SSE = QuantityRule(
    version="SSE_MAIN_QTY_1990_V1",
    market="SSE",
    board="main",
    effective_from=date(1990, 12, 19),
    buy_minimum=100,
    buy_increment=100,
    sell_increment=100,
    max_market_quantity=1_000_000,
    max_limit_quantity=1_000_000,
)
_MAIN_SZSE = QuantityRule(
    version="SZSE_MAIN_QTY_1991_V1",
    market="SZSE",
    board="main",
    effective_from=date(1991, 7, 3),
    buy_minimum=100,
    buy_increment=100,
    sell_increment=100,
    max_market_quantity=1_000_000,
    max_limit_quantity=1_000_000,
)
_CHINEXT_LEGACY = QuantityRule(
    version="SZSE_CHINEXT_QTY_2009_V1",
    market="SZSE",
    board="chinext",
    effective_from=date(2009, 10, 30),
    buy_minimum=100,
    buy_increment=100,
    sell_increment=100,
    max_market_quantity=1_000_000,
    max_limit_quantity=1_000_000,
)
_CHINEXT_REGISTRATION = QuantityRule(
    version="SZSE_CHINEXT_QTY_20200824_V2",
    market="SZSE",
    board="chinext",
    effective_from=date(2020, 8, 24),
    buy_minimum=100,
    buy_increment=100,
    sell_increment=100,
    max_market_quantity=150_000,
    max_limit_quantity=300_000,
)
_STAR = QuantityRule(
    version="SSE_STAR_QTY_20190722_V1",
    market="SSE",
    board="star",
    effective_from=date(2019, 7, 22),
    buy_minimum=200,
    buy_increment=1,
    sell_increment=1,
    max_market_quantity=50_000,
    max_limit_quantity=100_000,
)
_BSE = QuantityRule(
    version="BSE_QTY_20211115_V1",
    market="BSE",
    board="bse",
    effective_from=date(2021, 11, 15),
    buy_minimum=100,
    buy_increment=1,
    sell_increment=1,
    max_market_quantity=1_000_000,
    max_limit_quantity=1_000_000,
)


def quantity_rule(code: str, trade_date: date) -> QuantityRule:
    """按六位代码和成交日返回确定且可审计的申报规则版本。"""
    normalized = str(code).split(".")[0].zfill(6)
    if normalized.startswith(("688", "689")):
        if trade_date < _STAR.effective_from:
            raise ValueError("科创板成立前不存在可用申报规则")
        return _STAR
    if normalized.startswith("30"):
        return (
            _CHINEXT_REGISTRATION
            if trade_date >= _CHINEXT_REGISTRATION.effective_from
            else _CHINEXT_LEGACY
        )
    if normalized.startswith(("4", "8", "92")):
        if trade_date < _BSE.effective_from:
            raise ValueError("北交所成立前不存在可用申报规则")
        return _BSE
    if normalized.startswith("6"):
        return _MAIN_SSE
    if normalized.startswith(("00", "001", "002", "003")):
        return _MAIN_SZSE
    raise ValueError(f"无法识别证券 {code} 的交易板块")
