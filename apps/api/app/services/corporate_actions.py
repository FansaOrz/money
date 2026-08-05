"""A 股公司行为决策、税务、退市和零碎股的版本化规则。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
import math


@dataclass(frozen=True)
class TerminalResolution:
    action: str
    cash_per_share: float
    restricted_value_per_share: float
    requires_manual_review: bool
    reason: str
    rule_version: str = "TERMINAL_CONSIDERATION_V1"


def resolve_terminal(
    *,
    terminal_type: str | None,
    terminal_price: float | None,
    consideration_status: str,
    restricted_valuation_per_share: float = 0.0,
) -> TerminalResolution:
    """只有有官方最终对价的现金清算才可自动变现。"""
    kind = (terminal_type or "unknown").strip().lower()
    official = consideration_status in {"official", "verified"}
    if (
        kind in {"cash_liquidation", "cash_acquisition", "cash_option"}
        and official
        and terminal_price is not None
        and terminal_price >= 0
    ):
        return TerminalResolution(
            action="cash_settlement",
            cash_per_share=terminal_price,
            restricted_value_per_share=0.0,
            requires_manual_review=False,
            reason="官方最终现金对价已核验",
        )
    if kind in {"stock_swap", "merger"}:
        return TerminalResolution(
            action="await_stock_conversion",
            cash_per_share=0.0,
            restricted_value_per_share=max(restricted_valuation_per_share, 0.0),
            requires_manual_review=True,
            reason="等待官方换股比例和新证券登记",
        )
    if kind in {"transfer", "otc_transfer"}:
        reason = "转入退市板块/场外市场，保留受限资产"
    elif kind == "bankruptcy":
        reason = "破产清算最终回收未知，按保守值保留受限资产"
    elif kind in {"long_suspension", "frozen"}:
        reason = "长期冻结且无最终对价，保留受限资产"
    else:
        reason = "退市类型或最终对价未知，禁止按最后收盘价变现"
    return TerminalResolution(
        action="restrict_asset",
        cash_per_share=0.0,
        restricted_value_per_share=max(restricted_valuation_per_share, 0.0),
        requires_manual_review=True,
        reason=reason,
    )


@dataclass(frozen=True)
class DividendTaxRule:
    version: str
    effective_from: date
    effective_to: date | None
    within_month_rate: float
    month_to_year_rate: float
    over_year_rate: float
    withholding_timing: str = "sale_clawback"


DIVIDEND_TAX_RULES = (
    DividendTaxRule(
        version="CN_LISTED_DIVIDEND_TAX_2013",
        effective_from=date(2013, 1, 1),
        effective_to=date(2015, 9, 7),
        within_month_rate=0.20,
        month_to_year_rate=0.10,
        over_year_rate=0.05,
    ),
    DividendTaxRule(
        version="CN_LISTED_DIVIDEND_TAX_2015",
        effective_from=date(2015, 9, 8),
        effective_to=None,
        within_month_rate=0.20,
        month_to_year_rate=0.10,
        over_year_rate=0.0,
    ),
)


def dividend_tax_rule(entitlement_date: date) -> DividendTaxRule:
    for rule in reversed(DIVIDEND_TAX_RULES):
        if entitlement_date < rule.effective_from:
            continue
        if rule.effective_to is not None and entitlement_date > rule.effective_to:
            continue
        return rule
    raise ValueError(f"{entitlement_date} 缺少个人股息红利税规则")


def dividend_tax_rate(
    *,
    acquired_date: date,
    sale_date: date,
    entitlement_date: date,
) -> tuple[float, str, int]:
    if sale_date < acquired_date:
        raise ValueError("卖出日期不能早于买入日期")
    holding_days = (sale_date - acquired_date).days
    rule = dividend_tax_rule(entitlement_date)
    if holding_days <= 30:
        rate = rule.within_month_rate
    elif holding_days <= 365:
        rate = rule.month_to_year_rate
    else:
        rate = rule.over_year_rate
    return rate, rule.version, holding_days


@dataclass
class DividendTaxClaim:
    event_key: str
    code: str
    lot_id: str
    acquired_date: date
    entitlement_date: date
    remaining_shares: float
    gross_cash_per_share: float
    rule_version: str
    withheld_at_payment: float = 0.0


def create_dividend_tax_claims(
    *,
    code: str,
    event_key: str,
    entitlement_date: date,
    gross_cash_per_share: float,
    lots: list[object],
) -> list[DividendTaxClaim]:
    rule = dividend_tax_rule(entitlement_date)
    return [
        DividendTaxClaim(
            event_key=event_key,
            code=code,
            lot_id=str(getattr(lot, "lot_id")),
            acquired_date=getattr(lot, "acquired_date"),
            entitlement_date=entitlement_date,
            remaining_shares=float(getattr(lot, "shares")),
            gross_cash_per_share=gross_cash_per_share,
            rule_version=rule.version,
        )
        for lot in lots
        if float(getattr(lot, "shares")) > 0
        and gross_cash_per_share > 0
        and getattr(lot, "acquired_date") <= entitlement_date
    ]


def realize_dividend_tax(
    *,
    claims: list[DividendTaxClaim],
    consumed_lots: list[dict[str, object]],
    sale_date: date,
) -> tuple[float, list[dict[str, object]]]:
    """按 FIFO 实际卖出批次追缴历史分红税，并减少尚未出售的税务股数。"""
    tax_due = 0.0
    details: list[dict[str, object]] = []
    claims_by_lot: dict[str, list[DividendTaxClaim]] = {}
    for claim in claims:
        claims_by_lot.setdefault(claim.lot_id, []).append(claim)
    for consumed in consumed_lots:
        lot_id = str(consumed.get("lot_id") or "")
        sold_shares = float(consumed.get("shares") or 0.0)
        for claim in claims_by_lot.get(lot_id, ()):
            taxable_shares = min(sold_shares, claim.remaining_shares)
            if taxable_shares <= 0:
                continue
            rate, version, holding_days = dividend_tax_rate(
                acquired_date=claim.acquired_date,
                sale_date=sale_date,
                entitlement_date=claim.entitlement_date,
            )
            gross = taxable_shares * claim.gross_cash_per_share
            due = max(gross * rate - claim.withheld_at_payment, 0.0)
            claim.remaining_shares -= taxable_shares
            tax_due += due
            details.append(
                {
                    "event_key": claim.event_key,
                    "lot_id": claim.lot_id,
                    "shares": taxable_shares,
                    "holding_days": holding_days,
                    "tax_rate": rate,
                    "tax_due": due,
                    "rule_version": version,
                }
            )
    return tax_due, details


@dataclass(frozen=True)
class RightsDecisionPolicy:
    version: str = "RIGHTS_CASH_RESERVE_V1"
    mode: str = "maintain_pro_rata"
    minimum_cash_reserve_ratio: float = 0.05
    max_subscription_cash_ratio: float = 0.20


@dataclass(frozen=True)
class RightsDecision:
    requested_shares: float
    subscribed_shares: float
    sold_rights: float
    lapsed_rights: float
    subscription_cash: float
    rights_sale_cash: float
    reason: str
    policy_version: str


def decide_rights_issue(
    *,
    held_shares: float,
    subscription_ratio: float,
    subscription_price: float,
    available_cash: float,
    portfolio_value: float,
    rights_tradable: bool,
    right_market_price: float | None,
    policy: RightsDecisionPolicy = RightsDecisionPolicy(),
) -> RightsDecision:
    requested = max(held_shares * subscription_ratio, 0.0)
    if requested <= 0 or subscription_price <= 0:
        return RightsDecision(
            requested,
            0.0,
            0.0,
            requested,
            0.0,
            0.0,
            "配股字段无效，权利失效并等待人工核对",
            policy.version,
        )
    if policy.mode == "decline":
        affordable = 0.0
    else:
        cash_reserve = max(
            portfolio_value * policy.minimum_cash_reserve_ratio,
            0.0,
        )
        budget = min(
            max(available_cash - cash_reserve, 0.0),
            available_cash * policy.max_subscription_cash_ratio,
        )
        affordable = math.floor(budget / subscription_price)
    subscribed = min(requested, max(affordable, 0.0))
    residual = max(requested - subscribed, 0.0)
    sold_rights = residual if rights_tradable and (right_market_price or 0) > 0 else 0.0
    lapsed = residual - sold_rights
    return RightsDecision(
        requested_shares=requested,
        subscribed_shares=subscribed,
        sold_rights=sold_rights,
        lapsed_rights=lapsed,
        subscription_cash=subscribed * subscription_price,
        rights_sale_cash=sold_rights * float(right_market_price or 0.0),
        reason=(
            "按现金安全垫部分/全部认购，剩余权利按市场价出售"
            if sold_rights > 0
            else "按现金安全垫部分/全部认购，剩余权利失效"
            if residual > 0
            else "按现金安全垫全部认购"
        ),
        policy_version=policy.version,
    )


@dataclass(frozen=True)
class ShareConversion:
    registered_shares: float
    fractional_shares: float
    cash_compensation: float
    restricted_fractional_value: float
    rule_version: str = "CN_REGISTERED_INTEGER_FRACTION_V1"


def convert_registered_shares(
    *,
    raw_shares: float,
    cash_compensation_per_fraction: float | None,
    registration_increment: float = 1.0,
) -> ShareConversion:
    if raw_shares < 0 or registration_increment <= 0:
        raise ValueError("换股数量或登记增量无效")
    registered = (
        math.floor(raw_shares / registration_increment + 1e-12) * registration_increment
    )
    fractional = max(raw_shares - registered, 0.0)
    if cash_compensation_per_fraction is not None:
        cash = fractional * max(cash_compensation_per_fraction, 0.0)
        restricted = 0.0
    else:
        cash = 0.0
        restricted = fractional
    return ShareConversion(
        registered_shares=registered,
        fractional_shares=fractional,
        cash_compensation=cash,
        restricted_fractional_value=restricted,
    )
