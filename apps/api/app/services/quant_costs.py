"""交易成本模型（纯函数 + lot 查询接口）。

默认费率口径（简化稳健、可在请求中覆盖）：
- 买入（申购）：0.15%；
- 卖出（赎回）：持有 < 7 个自然日按 1.5%（惩罚性赎回费），否则 0.5%；
- 卖出费用基于 lot（每笔买入流水形成的份额批次），FIFO 先进先出：
  卖出份额依次消耗最早的在持 lot，按各 lot 买入日至卖出日的持有天数
  确定费率，加权得到该笔卖出的费用比例。

`load_open_lots` 由 Transaction 流水重建在持 lot（BUY/REINVEST 增加、
SELL 按 FIFO 扣减），供服务层调用；其余函数均为纯函数，便于单测。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Transaction, TransactionType

# 默认费率（小数）
DEFAULT_BUY_FEE_RATE = 0.0015
DEFAULT_SELL_FEE_RATE = 0.005
SHORT_TERM_SELL_FEE_RATE = 0.015
# 短持判定阈值：持有自然日 < 7 天按惩罚性费率
SHORT_TERM_HOLD_DAYS = 7


@dataclass(frozen=True)
class ShareLot:
    """一个在持份额批次：buy_date 买入 date、shares 剩余份额。"""

    buy_date: date
    shares: float


def buy_fee_rate(custom_rate: float | None = None) -> float:
    """买入费率（缺省 0.15%）。"""
    return DEFAULT_BUY_FEE_RATE if custom_rate is None else custom_rate


def sell_fee_rate(
    hold_days: int | None,
    default_rate: float = DEFAULT_SELL_FEE_RATE,
    short_term_rate: float = SHORT_TERM_SELL_FEE_RATE,
    short_term_days: int = SHORT_TERM_HOLD_DAYS,
) -> float:
    """卖出费率：持有 < short_term_days 个自然日按惩罚性费率，否则默认费率。

    hold_days 为 None（无法确定持有期，如无 lot 数据）或负数（数据异常，
    如卖出日早于 lot 买入日）时按默认费率 —— 负持有期不应触发惩罚性短持费率。
    """
    if hold_days is not None and 0 <= hold_days < short_term_days:
        return short_term_rate
    return default_rate


def estimate_sell_fee(
    lots: list[ShareLot],
    shares: float,
    sell_date: date,
    default_rate: float = DEFAULT_SELL_FEE_RATE,
    short_term_rate: float = SHORT_TERM_SELL_FEE_RATE,
    short_term_days: int = SHORT_TERM_HOLD_DAYS,
) -> tuple[float, float]:
    """按 FIFO 估算卖出 shares 份额的费用比例（小数）。

    依次消耗最早的在持 lot，各 lot 按买入日至 sell_date 的自然日数
    确定费率，份额加权平均；lots 为空或份额不足覆盖卖出量时，
    未覆盖部分按默认费率。返回 (费用比例, 实际纳入计算的份额)。
    """
    if shares <= 0:
        return 0.0, 0.0
    remaining = shares
    fee_weighted = 0.0
    covered = 0.0
    for lot in lots:
        if remaining <= 0:
            break
        take = min(lot.shares, remaining)
        if take <= 0:
            continue
        hold_days = (sell_date - lot.buy_date).days
        rate = sell_fee_rate(hold_days, default_rate, short_term_rate, short_term_days)
        fee_weighted += take * rate
        covered += take
        remaining -= take
    if remaining > 0:
        # lot 数据不足覆盖卖出量：缺口按默认费率
        fee_weighted += remaining * default_rate
        covered += remaining
    if covered <= 0:
        return default_rate, 0.0
    return fee_weighted / covered, covered


def apply_costs_to_returns(
    returns: list[float],
    trades: list[tuple[int, float, float]],
) -> list[float]:
    """在策略日收益序列上扣除交易费用（纯函数）。

    trades 为 (日下标, 买入比例, 卖出比例) 列表：买入/卖出比例均为
    相对当日组合总价值的小数（0~1），费用 = 买比例×买费率 + 卖比例×
    卖费率，从当日收益中扣除：r' = (1 + r) × (1 - fee) - 1。
    返回新序列，不修改入参。
    """
    result = list(returns)
    for index, buy_ratio, sell_ratio in trades:
        if not 0 <= index < len(result):
            continue
        fee = buy_ratio * DEFAULT_BUY_FEE_RATE + sell_ratio * DEFAULT_SELL_FEE_RATE
        if fee <= 0:
            continue
        result[index] = (1.0 + result[index]) * (1.0 - fee) - 1.0
    return result


# ---------------------------------------------------------------------------
# lot 查询接口（由交易流水重建在持份额批次）
# ---------------------------------------------------------------------------


def load_open_lots(db: Session, instrument_id: int) -> list[ShareLot]:
    """由 Transaction 流水重建某基金的在持 lot（FIFO，按交易日升序）。

    BUY / REINVEST（红利再投）增加 lot；SELL 按 FIFO 扣减最早 lot；
    其余类型（分红/费用/转托管等）不影响份额批次。份额为 0 的流水跳过。
    """
    rows = db.execute(
        select(Transaction.type, Transaction.trade_date, Transaction.shares)
        .where(Transaction.instrument_id == instrument_id)
        .order_by(Transaction.trade_date, Transaction.id)
    ).all()

    lots: list[ShareLot] = []
    for tx_type, trade_date, shares in rows:
        if trade_date is None or shares is None:
            continue
        amount = float(Decimal(shares))
        if amount == 0:
            continue
        if tx_type in (TransactionType.BUY, TransactionType.REINVEST):
            lots.append(ShareLot(buy_date=trade_date, shares=abs(amount)))
        elif tx_type == TransactionType.SELL:
            remaining = abs(amount)
            while remaining > 0 and lots:
                head = lots[0]
                take = min(head.shares, remaining)
                head_shares = head.shares - take
                remaining -= take
                if head_shares <= 1e-12:
                    lots.pop(0)
                else:
                    lots[0] = ShareLot(buy_date=head.buy_date, shares=head_shares)
    return lots
