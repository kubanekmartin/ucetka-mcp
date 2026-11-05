"""Roční přehled OSVČ: příjmy, DPH po obdobích, odhad daně a pojistného vs. zálohy, termíny."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import ROUND_CEILING, Decimal
from typing import Literal

from ucetka import expenses as expenses_mod
from ucetka import importer, ledger
from ucetka.config import load_supplier
from ucetka.calendar_cz import next_working_day
from ucetka.payments import TaxSettings, load_payments, load_settings, paid_total
from ucetka.tax import (
    IncomeTax,
    Insurance,
    flat_rate_expenses,
    health_insurance_no_minimum,
    income_tax,
    social_insurance_side_activity,
)
from ucetka.tax_params import PARAMS, TaxParams, params_for

ZERO = Decimal(0)


@dataclass
class MonthRow:
    month: int
    issued_net: Decimal = ZERO
    issued_vat: Decimal = ZERO
    paid_net: Decimal = ZERO
    expense_vat: Decimal = ZERO
    paid_expenses: Decimal = ZERO
    previous_year_issued_net: Decimal = ZERO


@dataclass
class VatPeriod:
    label: str
    start: date
    end: date
    output_vat: Decimal
    input_vat: Decimal
    due: date

    @property
    def result(self) -> Decimal:
        return self.output_vat - self.input_vat


@dataclass
class Estimate:
    kind: str
    label: str
    expected: Decimal
    paid: Decimal
    planned: Decimal
    note: str

    @property
    def remaining(self) -> Decimal:
        """Kladné = doplatek po zaplacení plánovaných záloh, záporné = přeplatek."""
        return self.expected - self.paid - self.planned


@dataclass
class Deadline:
    when: date
    what: str


@dataclass
class YearOverview:
    year: int
    today: date
    params: TaxParams
    settings: TaxSettings
    months: list[MonthRow]
    issued_net: Decimal
    paid_net: Decimal
    expected_income: Decimal
    expected_expenses: Decimal
    unpaid: list[ledger.InvoiceRecord]
    tax: IncomeTax
    social: Insurance
    health: Insurance
    estimates: list[Estimate]
    vat_periods: list[VatPeriod]
    deadlines: list[Deadline]
    invoice_issues: list[importer.ImportIssue] = field(default_factory=list)
    expense_issues: list[expenses_mod.ExpenseIssue] = field(default_factory=list)

    @property
    def overdue(self) -> list[ledger.InvoiceRecord]:
        return [r for r in self.unpaid if not r.invoice.is_credit_note and r.invoice.due_date < self.today]

    @property
    def effective(self) -> EffectiveRate:
        return EffectiveRate.of(self.tax, self.social, self.health)

    @property
    def completed_months(self) -> int:
        if self.today.year > self.year:
            return 12
        return self.today.month - 1 if self.today.year == self.year else 0


@dataclass
class EffectiveRate:
    """Daň + pojistné (bez DPH, ta je průběžná) vůči příjmům a zisku."""

    burden: Decimal
    income: Decimal
    profit: Decimal

    @classmethod
    def of(cls, tax: IncomeTax, social: Insurance, health: Insurance) -> EffectiveRate:
        return cls(tax.tax + social.amount + health.amount, tax.income, tax.profit)

    @property
    def on_income(self) -> Decimal:
        return self.burden / self.income if self.income else ZERO

    @property
    def on_profit(self) -> Decimal:
        return self.burden / self.profit if self.profit else ZERO


@dataclass
class Forecast:
    method: str
    basis_months: int
    run_rate: Decimal
    income: Decimal
    expense_run_rate: Decimal
    tax: IncomeTax
    social: Insurance
    health: Insurance

    @property
    def effective(self) -> EffectiveRate:
        return EffectiveRate.of(self.tax, self.social, self.health)


@dataclass
class Limit:
    label: str
    threshold: Decimal
    actual: Decimal
    forecast: Decimal | None
    note: str

    @property
    def share(self) -> Decimal:
        return self.actual / self.threshold

    @property
    def forecast_share(self) -> Decimal | None:
        return None if self.forecast is None else self.forecast / self.threshold


@dataclass
class AdvanceMonth:
    period: str
    due: date
    amount: Decimal
    paid: Decimal
    status: Literal["paid", "overdue", "due", "future"]


@dataclass
class NextYearAdvances:
    social_monthly: Decimal
    social_note: str
    dpfo_note: str


def _quarters(year: int) -> list[tuple[str, date, date]]:
    out = []
    for q in range(4):
        start = date(year, 3 * q + 1, 1)
        end = (
            date(year + (q == 3), (3 * q + 4) % 12 or 12, 1)
            if q < 3
            else date(year + 1, 1, 1)
        ) - timedelta(days=1)
        out.append((f"Q{q + 1}", start, end))
    return out


def _months(year: int) -> list[tuple[str, date, date]]:
    out = []
    for m in range(1, 13):
        end = (date(year + (m == 12), m % 12 + 1, 1)) - timedelta(days=1)
        out.append((f"{m:02d}/{year}", date(year, m, 1), end))
    return out


def _vat_periods(year: int, settings: TaxSettings) -> list[VatPeriod]:
    records = [r for r in ledger.list_records() if r.status != "cancelled"]
    expenses = expenses_mod.list_expenses()
    periods = _quarters(year) if settings.vat_period == "quarterly" else _months(year)
    out = []
    for label, start, end in periods:
        output = sum(
            (
                r.invoice.sign * r.invoice.total_vat_czk
                for r in records
                if start <= r.invoice.tax_point_date <= end
            ),
            ZERO,
        )
        output += sum(
            (
                e.vat_total
                for e in expenses
                if e.reverse_charge and start <= e.tax_point_date <= end
            ),
            ZERO,
        )
        input_vat = sum(
            (e.vat_total for e in expenses if start <= e.vat_date <= end), ZERO
        )
        due = next_working_day(end + timedelta(days=25))
        out.append(VatPeriod(label, start, end, output, input_vat, due))
    return out


def advance_schedule(year: int, today: date, amount: Decimal, kind: str = "cssz") -> list[AdvanceMonth]:
    """Zálohy ČSSZ jsou splatné do posledního dne měsíce, za který se platí (cssz.gov.cz/osvc-v-kostce).

    Platba se páruje podle `period`; starší zápisy bez něj podle měsíce úhrady.
    """
    paid: dict[str, Decimal] = {}
    for p in load_payments(year):
        if p.kind == kind and (p.period or p.paid_on.year == year):
            key = p.period or p.paid_on.strftime("%Y-%m")
            paid[key] = paid.get(key, ZERO) + p.amount
    out = []
    for m in range(1, 13):
        period = f"{year}-{m:02d}"
        due = date(year + (m == 12), m % 12 + 1, 1) - timedelta(days=1)
        if period in paid:
            status = "paid"
        elif due < today:
            status = "overdue"
        elif date(year, m, 1) <= today:
            status = "due"
        else:
            status = "future"
        out.append(AdvanceMonth(period, due, amount, paid.get(period, ZERO), status))
    return out


def _unpaid_advance_months(year: int, today: date, amount: Decimal, kind: str = "cssz") -> int:
    if not amount:
        return 0
    return sum(1 for m in advance_schedule(year, today, amount, kind) if m.status != "paid")


def _health_note(settings: TaxSettings, p: TaxParams) -> str:
    if settings.health_status == "employee":
        return "Zaměstnanec: zálohy ani minimum se neplatí, jen doplatek 13,5 % z 50 % zisku po přehledu."
    if not p.student_health_advances:
        return "Student: od 2026 bez záloh, platí se doplatek po přehledu."
    return "Student: zálohy podle přehledu, bez minima; 13,5 % z 50 % zisku."


def forecast(o: YearOverview, method: Literal["ytd", "3m"] = "ytd") -> Forecast | None:
    """Doběh roku: co je vystaveno/zaplaceno + průměr fakturace za dokončené měsíce × zbytek roku.

    Běžný měsíc se doplní do průměru, pokud je zatím pod ním.
    """
    if o.today.year != o.year or o.completed_months == 0:
        return None
    done = o.months[: o.completed_months]
    basis = done if method == "ytd" else done[-3:]
    future = 12 - o.today.month
    current = o.months[o.today.month - 1]

    def extrapolate(so_far: Decimal, per_month: list[Decimal], this_month: Decimal) -> tuple[Decimal, Decimal]:
        rate = sum(per_month, ZERO) / len(per_month)
        return so_far + max(rate - this_month, ZERO) + rate * future, rate

    income, run_rate = extrapolate(o.expected_income, [m.issued_net for m in basis], current.issued_net)
    actual_expenses, expense_rate = extrapolate(
        o.expected_expenses, [m.paid_expenses for m in basis], current.paid_expenses
    )
    tax = _income_tax(income, actual_expenses, o.params, o.settings)
    return Forecast(
        method, len(basis), run_rate, income, expense_rate if o.settings.expense_mode == "actual" else ZERO, tax,
        social_insurance_side_activity(tax.profit, o.params),
        health_insurance_no_minimum(tax.profit, o.params),
    )


def _income_tax(income: Decimal, actual_expenses: Decimal, p: TaxParams, settings: TaxSettings) -> IncomeTax:
    expenses = (
        actual_expenses
        if settings.expense_mode == "actual"
        else flat_rate_expenses(income, p, settings.flat_rate_percent)
    )
    return income_tax(
        income, expenses, p, deductions=settings.deductions, employment_income=settings.employment_income
    )


def limits(o: YearOverview, fc: Forecast | None, *, vat_payer: bool) -> list[Limit]:
    cap_income = o.params.flat_rate_expense_cap_60 * 100 / 60
    out = [] if o.settings.expense_mode == "actual" else [
        Limit(
            "Strop paušálu", cap_income, o.expected_income, fc.income if fc else None,
            f"Nad {cap_income:,.0f} Kč příjmů už výdaje nerostou (max. "
            f"{o.params.flat_rate_expense_cap_60 * o.settings.flat_rate_percent / 60:,.0f} Kč) — daní se celý zbytek.".replace(",", " "),
        ),
    ]
    out += [
        Limit(
            "Hranice 23 % daně", o.params.higher_rate_threshold, o.tax.tax_base, fc.tax.tax_base if fc else None,
            "Základ daně (vč. mzdy) nad hranicí se daní 23 % místo 15 %.",
        ),
    ]
    if not vat_payer:
        out.append(Limit(
            "Registrace k DPH", o.params.vat_registration_limit, o.issued_net, None,
            "Obrat za kalendářní rok — po překročení registrace k DPH.",
        ))
    return out


def _dpfo_regime(last_tax: Decimal) -> str:
    if last_tax > 150_000:
        return "čtvrtletní zálohy na daň (4 × 25 % daně), splatné 15. 3., 6., 9. a 12"
    if last_tax > 30_000:
        return "pololetní zálohy na daň (2 × 40 % daně), splatné 15. 6. a 15. 12"
    return "bez záloh na daň (daň do 30 000 Kč)"


def next_year_advances(o: YearOverview, fc: Forecast | None) -> NextYearAdvances:
    tax, social = (fc.tax, fc.social) if fc else (o.tax, o.social)
    if not social.applies:
        social_monthly, note = ZERO, "Zisk pod rozhodnou částkou — zálohy na sociální se neplatí."
    else:
        base = (tax.profit * o.params.social_base_share / 12).quantize(Decimal(1), rounding=ROUND_CEILING)
        social_monthly = (base * o.params.social_rate).quantize(Decimal(1), rounding=ROUND_CEILING)
        nxt = PARAMS.get(o.year + 1)
        if nxt is None:
            note = f"Minimální záloha pro {o.year + 1} ještě není zadaná — částka bez porovnání s minimem."
        else:
            social_monthly = max(social_monthly, nxt.social_side_min_advance)
            note = f"Vč. minima {nxt.social_side_min_advance} Kč pro vedlejší činnost {o.year + 1}."
    return NextYearAdvances(social_monthly, note, f"Podle odhadované daně {tax.tax:,.0f} Kč: {_dpfo_regime(tax.tax)}.".replace(",", " "))


def _deadlines(
    year: int, today: date, settings: TaxSettings, vat_periods: list[VatPeriod]
) -> list[Deadline]:
    items = [
        Deadline(p.due, f"DPH + kontrolní hlášení za {p.label}/{year}")
        for p in vat_periods
    ]
    dpfo = next_working_day(date(year + 1, 5, 1))
    items.append(Deadline(dpfo, f"Daňové přiznání {year} (elektronicky)"))
    prehled = next_working_day(date(year + 1, 6, 1))
    items.append(
        Deadline(prehled, f"Přehledy ČSSZ a ZP za {year}; doplatky do 8 dnů od podání")
    )
    if settings.last_tax_liability > 150_000:
        items += [
            Deadline(next_working_day(date(year, m, 15)), "Záloha na daň (25 %)")
            for m in (3, 6, 9, 12)
        ]
    elif settings.last_tax_liability > 30_000:
        items += [
            Deadline(next_working_day(date(year, m, 15)), "Záloha na daň (40 %)")
            for m in (6, 12)
        ]
    if settings.social_advance_monthly:
        end_of_month = (
            date(today.year + (today.month == 12), today.month % 12 + 1, 1)
        ) - timedelta(days=1)
        items.append(
            Deadline(
                end_of_month,
                f"Záloha ČSSZ {settings.social_advance_monthly} Kč za {today.month:02d}",
            )
        )
    return sorted((d for d in items if d.when >= today), key=lambda d: d.when)


def build(year: int, today: date | None = None, *, sync: bool = True) -> YearOverview:
    today = today or date.today()
    if sync:
        importer.sync()
        expenses_mod.sync()
    p = params_for(year)
    settings = load_settings()
    records = [r for r in ledger.list_records() if r.status != "cancelled"]

    months = [MonthRow(m) for m in range(1, 13)]
    for r in records:
        inv = r.invoice
        if inv.tax_point_date.year == year:
            months[inv.tax_point_date.month - 1].issued_net += inv.sign * inv.total_net_czk
            months[inv.tax_point_date.month - 1].issued_vat += inv.sign * inv.total_vat_czk
        if inv.tax_point_date.year == year - 1:
            months[
                inv.tax_point_date.month - 1
            ].previous_year_issued_net += inv.sign * inv.total_net_czk
        if r.paid_date and r.paid_date.year == year:
            months[r.paid_date.month - 1].paid_net += inv.sign * inv.total_net_czk
    vat_payer = load_supplier().vat_payer
    expected_expenses = ZERO
    for e in expenses_mod.list_expenses():
        if e.tax_point_date.year == year:
            months[e.tax_point_date.month - 1].expense_vat += e.vat_total
        cost = expenses_mod.income_tax_cost(e, vat_payer=vat_payer)
        if e.paid_date and e.paid_date.year == year:
            months[e.paid_date.month - 1].paid_expenses += cost
            expected_expenses += cost
        elif e.paid_date is None and e.issue_date.year == year:
            expected_expenses += cost

    unpaid = [
        r for r in records if r.status == "issued" and r.invoice.issue_date.year <= year
    ]
    paid_net = sum((m.paid_net for m in months), ZERO)
    # Daň z příjmů OSVČ jde podle zaplacených faktur (i výdajů). Nezaplacené letošní počítáme, jako by se zaplatily letos.
    expected_income = paid_net + sum(
        (r.invoice.sign * r.invoice.total_net_czk for r in unpaid if r.invoice.issue_date.year == year),
        ZERO,
    )
    tax = _income_tax(expected_income, expected_expenses, p, settings)
    social = social_insurance_side_activity(tax.profit, p)
    health = health_insurance_no_minimum(tax.profit, p)

    months_left = _unpaid_advance_months(year, today, settings.social_advance_monthly)
    health_months_left = _unpaid_advance_months(year, today, settings.health_advance_monthly, "zp")
    estimates = [
        Estimate(
            "dpfo",
            "Daň z příjmů",
            tax.tax,
            paid_total("dpfo", year),
            ZERO,
            "Bez záloh — poslední daň ≤ 30 000 Kč."
            if settings.last_tax_liability <= 30_000
            else "Zálohy podle § 38a.",
        ),
        Estimate(
            "cssz",
            "Sociální pojištění",
            social.amount,
            paid_total("cssz", year),
            settings.social_advance_monthly * months_left,
            "Vedlejší činnost: 29,2 % z 55 % zisku."
            if social.applies
            else f"Zisk pod rozhodnou částkou {p.social_side_threshold:,.0f} Kč — nic.".replace(
                ",", " "
            ),
        ),
        Estimate(
            "zp",
            "Zdravotní pojištění",
            health.amount,
            paid_total("zp", year),
            settings.health_advance_monthly * health_months_left,
            _health_note(settings, p),
        ),
    ]
    vat_periods = _vat_periods(year, settings)
    return YearOverview(
        year=year,
        today=today,
        params=p,
        settings=settings,
        months=months,
        issued_net=sum((m.issued_net for m in months), ZERO),
        paid_net=paid_net,
        expected_income=expected_income,
        expected_expenses=expected_expenses,
        unpaid=sorted(unpaid, key=lambda r: r.invoice.due_date),
        tax=tax,
        social=social,
        health=health,
        estimates=estimates,
        vat_periods=vat_periods,
        deadlines=_deadlines(year, today, settings, vat_periods),
        invoice_issues=importer.load_issues(),
        expense_issues=expenses_mod.load_issues(),
    )
