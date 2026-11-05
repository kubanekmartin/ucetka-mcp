"""Odhad daně z příjmů a pojistného OSVČ — s paušálními i skutečnými výdaji."""
from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal

from ucetka.tax_params import TaxParams


def _ceil(value: Decimal) -> Decimal:
    return value.quantize(Decimal(1), rounding=ROUND_CEILING)


def _floor_hundreds(value: Decimal) -> Decimal:
    return (value / 100).quantize(Decimal(1), rounding=ROUND_FLOOR) * 100


@dataclass(frozen=True)
class IncomeTax:
    income: Decimal
    expenses: Decimal
    profit: Decimal
    tax_base: Decimal
    tax_before_credit: Decimal
    tax: Decimal


@dataclass(frozen=True)
class Insurance:
    assessment_base: Decimal
    amount: Decimal
    applies: bool


def flat_rate_expenses(income: Decimal, p: TaxParams, percent: int) -> Decimal:
    """Výdajový paušál § 7 odst. 7 ZDP se stropem (60 % → max. 1 200 000 Kč, tj. příjem 2 mil.)."""
    return min(_ceil(income * percent / 100), p.flat_rate_expense_cap_60 * percent / 60)


def _tax_on(base: Decimal, p: TaxParams) -> Decimal:
    over = max(base - p.higher_rate_threshold, Decimal(0))
    return (base - over) * p.income_tax_rate + over * p.higher_income_tax_rate


def income_tax(
    income: Decimal,
    expenses: Decimal,
    p: TaxParams,
    *,
    deductions: Decimal = Decimal(0),
    employment_income: Decimal = Decimal(0),
) -> IncomeTax:
    """§ 7 ZDP. Základ po odpočtech se zaokrouhluje dolů na stovky (§ 16).

    Se mzdou (§ 6) vrací jen daň, kterou přidá podnikání: daň(mzda + zisk) − daň(mzda) —
    slevu na poplatníka už uplatnil zaměstnavatel, podruhé ji odečíst nejde.
    """
    profit = max(income - expenses, Decimal(0))
    tax_base = _floor_hundreds(max(employment_income + profit - deductions, Decimal(0)))
    tax_before = _tax_on(tax_base, p)
    tax = max(tax_before - p.taxpayer_credit, Decimal(0))
    if employment_income:
        employment_base = _floor_hundreds(employment_income)
        tax_before -= _tax_on(employment_base, p)
        tax -= max(_tax_on(employment_base, p) - p.taxpayer_credit, Decimal(0))
    return IncomeTax(income, expenses, _ceil(profit), tax_base, _ceil(tax_before), _ceil(tax))


def social_insurance_side_activity(profit: Decimal, p: TaxParams) -> Insurance:
    """Vedlejší činnost: pojistné jen při zisku nad rozhodnou částkou."""
    if profit <= p.social_side_threshold:
        return Insurance(Decimal(0), Decimal(0), applies=False)
    base = min(_ceil(profit * p.social_base_share), p.social_max_base)
    return Insurance(base, _ceil(base * p.social_rate), applies=True)


def health_insurance_no_minimum(profit: Decimal, p: TaxParams) -> Insurance:
    """Student (státní pojištěnec) i zaměstnanec: minimální vyměřovací základ OSVČ neplatí (VZP)."""
    base = _ceil(profit * p.health_base_share)
    return Insurance(base, _ceil(base * p.health_rate), applies=profit > 0)
