"""中国 A 股按市场、方向和生效日期版本化的交易费用规则。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True)
class FeeSchedule:
    version: str
    effective_from: date
    stamp_tax_sell_rate: float
    transfer_fee_rate: float
    transfer_fee_basis: str = "amount"


@dataclass(frozen=True)
class FeeBreakdown:
    rule_version: str
    commission: float
    stamp_tax: float
    transfer_fee: float
    total: float


def _market(code: str) -> str:
    normalized = str(code).split(".")[0].zfill(6)
    if normalized.startswith(("4", "8", "92")):
        return "BSE"
    if normalized.startswith("6"):
        return "SSE"
    if normalized.startswith(("00", "30")):
        return "SZSE"
    raise ValueError(f"无法识别证券 {code} 的费用市场")


def fee_schedule(code: str, trade_date: date) -> FeeSchedule:
    """返回适用政策版本；2012 年以前因缺少统一可靠口径而拒绝猜测。"""
    market = _market(code)
    if trade_date < date(2012, 9, 1):
        raise ValueError("2012-09-01 前费用口径未受治理，禁止正式回测")
    stamp_rate = 0.0005 if trade_date >= date(2023, 8, 28) else 0.001
    stamp_version = (
        "STAMP_20230828_HALF" if stamp_rate == 0.0005 else "STAMP_20080919_SELL"
    )
    if trade_date >= date(2022, 4, 29):
        transfer_rate = 0.00001
        transfer_version = "TRANSFER_20220429_ALL_0.01_PERMILLE"
    elif trade_date >= date(2015, 8, 1):
        transfer_rate = 0.00002 if market != "BSE" else 0.000025
        transfer_version = (
            "TRANSFER_20150801_HS_0.02_PERMILLE"
            if market != "BSE"
            else "TRANSFER_BSE_0.025_PERMILLE"
        )
    elif market == "SSE":
        # 2012 调整后的沪市口径按成交面值 0.3‰ 双向收取。
        transfer_rate = 0.0003
        transfer_version = "TRANSFER_SSE_20120901_FACE_0.3_PERMILLE"
        return FeeSchedule(
            version=f"{stamp_version}+{transfer_version}",
            effective_from=date(2012, 9, 1),
            stamp_tax_sell_rate=stamp_rate,
            transfer_fee_rate=transfer_rate,
            transfer_fee_basis="face_value",
        )
    else:
        transfer_rate = 0.0000255
        transfer_version = "TRANSFER_SZSE_20120901_0.0255_PERMILLE"
    return FeeSchedule(
        version=f"{stamp_version}+{transfer_version}",
        effective_from=(
            date(2023, 8, 28)
            if trade_date >= date(2023, 8, 28)
            else date(2022, 4, 29)
            if trade_date >= date(2022, 4, 29)
            else date(2015, 8, 1)
            if trade_date >= date(2015, 8, 1)
            else date(2012, 9, 1)
        ),
        stamp_tax_sell_rate=stamp_rate,
        transfer_fee_rate=transfer_rate,
    )

def calculate_fee(
    *,
    code: str,
    trade_date: date,
    side: str,
    amount: float,
    shares: float,
    commission_rate: float,
    minimum_commission: float,
) -> FeeBreakdown:
    """计算佣金、印花税和过户费，不把任何政策项隐藏进经验常数。"""
    if side not in {"buy", "sell"}:
        raise ValueError("side 必须为 buy/sell")
    schedule = fee_schedule(code, trade_date)
    commission = max(max(amount, 0.0) * commission_rate, minimum_commission)
    stamp_tax = (
        max(amount, 0.0) * schedule.stamp_tax_sell_rate
        if side == "sell"
        else 0.0
    )
    transfer_basis = (
        max(shares, 0.0)
        if schedule.transfer_fee_basis == "face_value"
        else max(amount, 0.0)
    )
    transfer_fee = transfer_basis * schedule.transfer_fee_rate
    total = commission + stamp_tax + transfer_fee
    return FeeBreakdown(
        rule_version=schedule.version,
        commission=commission,
        stamp_tax=stamp_tax,
        transfer_fee=transfer_fee,
        total=total,
    )
