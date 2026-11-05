from datetime import date
from decimal import Decimal
from pathlib import Path

import yaml
from fastapi.testclient import TestClient

from ucetka import expenses, ledger, overview
from ucetka.config import load_supplier
from ucetka.issue import issue
from ucetka.models import Invoice
from ucetka.payments import Payment, record_payment

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"


def _issue(number: str, d: date, price: str) -> None:
    data = yaml.safe_load((EXAMPLES / "2605001.yaml").read_text())
    data["supplier"] = load_supplier(EXAMPLES / "supplier.yaml").party().model_dump()
    data.update(number=number, issue_date=d, tax_point_date=d, due_date=d)
    data["payment"]["variable_symbol"] = number
    data["lines"][0]["unit_price_net"] = price
    issue(Invoice.model_validate(data))


def _setup(ucetka_home: Path) -> None:
    (ucetka_home / "tax.yaml").write_text("social_advance_monthly: 3500\nlast_tax_liability: 13800\n")
    _issue("2602001", date(2026, 2, 10), "100000")
    _issue("2605001", date(2026, 5, 10), "200000")
    ledger.mark_paid("2602001", date(2026, 2, 20))
    expenses.save(expenses.Expense(
        id="2602900", document_number="A1", supplier_name="Alza.cz a.s.", issue_date=date(2026, 2, 1),
        tax_point_date=date(2026, 2, 1), vat_rows=[expenses.VatRow(rate=Decimal(21), base=Decimal(1000), vat=Decimal(210))],
        total=Decimal(1210),
    ))
    expenses.save(expenses.Expense(
        id="2605900", document_number="G1", supplier_name="Google Cloud", issue_date=date(2026, 5, 1),
        tax_point_date=date(2026, 5, 1), vat_rows=[expenses.VatRow(rate=Decimal(21), base=Decimal(500), vat=Decimal(105))],
        total=Decimal(500), reverse_charge=True, supplier_country="IE",
    ))
    record_payment(Payment(kind="cssz", amount=Decimal(3500), paid_on=date(2026, 10, 1), tax_year=2026))


def test_year_overview(ucetka_home: Path) -> None:
    _setup(ucetka_home)
    o = overview.build(2026, date(2026, 10, 4), sync=False)
    assert o.issued_net == Decimal(300_000)
    assert o.paid_net == Decimal(100_000)
    assert o.expected_income == Decimal(300_000)
    assert o.tax.profit == Decimal(120_000)
    assert o.social.applies
    q1, q2 = o.vat_periods[0], o.vat_periods[1]
    assert q1.output_vat == Decimal(21_000) and q1.input_vat == Decimal(210)
    assert q2.output_vat == Decimal(42_105) and q2.input_vat == Decimal(105)
    assert str(q2.due) == "2026-07-27"
    cssz = next(e for e in o.estimates if e.kind == "cssz")
    assert cssz.paid == Decimal(3500)
    assert cssz.planned == Decimal(3500 * 11)
    assert [r.invoice.number for r in o.unpaid] == ["2605001"]


def test_dashboard_renders(ucetka_home: Path) -> None:
    _setup(ucetka_home)
    from ucetka.web.app import app

    r = TestClient(app).get("/prehled?year=2026")
    assert r.status_code == 200
    assert "Přehled 2026" in r.text
    assert "2605001" in r.text
    assert "Efektivní zdanění" in r.text
    assert "limit DPH" not in r.text


def test_effective_rate_and_forecast(ucetka_home: Path) -> None:
    _setup(ucetka_home)
    o = overview.build(2026, date(2026, 10, 4), sync=False)
    eff = o.effective
    assert eff.burden == o.tax.tax + o.social.amount + o.health.amount
    assert eff.on_income == eff.burden / Decimal(300_000)

    fc = overview.forecast(o, "ytd")
    assert fc.basis_months == 9
    assert fc.run_rate == Decimal(300_000) / 9
    assert fc.income == Decimal(300_000) + fc.run_rate * 3
    fc3 = overview.forecast(o, "3m")
    assert (fc3.basis_months, fc3.run_rate, fc3.income) == (3, Decimal(0), Decimal(300_000))


def test_no_forecast_for_past_year_or_january(ucetka_home: Path) -> None:
    _setup(ucetka_home)
    assert overview.forecast(overview.build(2025, date(2026, 10, 4), sync=False)) is None
    assert overview.forecast(overview.build(2026, date(2026, 1, 15), sync=False)) is None


def test_limits_are_flat_rate_cap_and_higher_bracket(ucetka_home: Path) -> None:
    _setup(ucetka_home)
    o = overview.build(2026, date(2026, 10, 4), sync=False)
    lims = overview.limits(o, overview.forecast(o), vat_payer=True)
    assert [lim.threshold for lim in lims] == [Decimal(2_000_000), Decimal(1_762_812)]
    assert lims[0].actual == Decimal(300_000)
    assert len(overview.limits(o, None, vat_payer=False)) == 3


def test_advance_schedule_matches_by_period(ucetka_home: Path) -> None:
    (ucetka_home / "tax.yaml").write_text("social_advance_monthly: 3500\n")
    record_payment(Payment(kind="cssz", amount=Decimal(3500), paid_on=date(2026, 11, 3), tax_year=2026, period="2026-10"))
    record_payment(Payment(kind="cssz", amount=Decimal(3500), paid_on=date(2026, 9, 20), tax_year=2026))
    months = {m.period: m.status for m in overview.advance_schedule(2026, date(2026, 11, 10), Decimal(3500))}
    assert months["2026-10"] == "paid"
    assert months["2026-09"] == "paid"
    assert months["2026-08"] == "overdue"
    assert months["2026-11"] == "due"
    assert months["2026-12"] == "future"


def test_next_year_advance(ucetka_home: Path) -> None:
    _setup(ucetka_home)
    o = overview.build(2026, date(2026, 10, 4), sync=False)
    nxt = overview.next_year_advances(o, None)
    assert nxt.social_monthly == Decimal(1606)
    assert "2027" in nxt.social_note
    assert "bez záloh" in nxt.dpfo_note


def test_taxes_and_advances_pages(ucetka_home: Path) -> None:
    _setup(ucetka_home)
    (ucetka_home / "tax.yaml").write_text(
        "social_advance_monthly: 3500\ncssz_payment:\n  account: 1234567899/0800\n  variable_symbol: '1234567890'\n"
    )
    from ucetka.web.app import app

    client = TestClient(app)
    for url, text in [("/dane", "Strop paušálu"), ("/zalohy", "Rozpis záloh ČSSZ"), ("/dph", "Po obdobích")]:
        r = client.get(url + "?year=2026")
        assert r.status_code == 200 and text in r.text, url
    assert 'alt="QR platba zálohy' in client.get("/zalohy?year=2026").text

    r = client.post("/zalohy/zaplaceno", data={"period": "2026-03", "amount": "3500", "paid_on": "2026-03-20"},
                    follow_redirects=False)
    assert r.status_code == 303
    from ucetka.payments import load_payments
    assert load_payments(2026)[-1].period == "2026-03"


def _actual_profile(home: Path, extra: str = "") -> None:
    (home / "tax.yaml").write_text("expense_mode: actual\nhealth_status: employee\n" + extra)


def _paid_expense(id_: str, issued: date, base: str, paid: date | None) -> None:
    b = Decimal(base)
    expenses.save(expenses.Expense(
        id=id_, document_number=id_, supplier_name="Dodavatel", supplier_dic="CZ27082440", issue_date=issued,
        tax_point_date=issued, vat_rows=[expenses.VatRow(rate=Decimal(21), base=b, vat=b * Decimal("0.21"))],
        total=b * Decimal("1.21"), paid_date=paid,
    ))


def test_actual_expenses_mode(ucetka_home: Path) -> None:
    _actual_profile(ucetka_home)
    _issue("2602001", date(2026, 2, 10), "100000")
    ledger.mark_paid("2602001", date(2026, 2, 20))
    _paid_expense("e1", date(2026, 2, 1), "30000", date(2026, 2, 5))
    _paid_expense("e2", date(2026, 3, 1), "10000", None)
    _paid_expense("e3", date(2025, 12, 1), "5000", date(2025, 12, 3))
    o = overview.build(2026, date(2026, 10, 4), sync=False)
    assert o.expected_expenses == Decimal(40_000)
    assert o.tax.expenses == Decimal(40_000)
    assert o.tax.profit == Decimal(60_000)
    assert o.months[1].paid_expenses == Decimal(30_000)
    assert [lim.label for lim in overview.limits(o, None, vat_payer=True)] == ["Hranice 23 % daně"]
    zp = next(e for e in o.estimates if e.kind == "zp")
    assert zp.planned == 0 and "Zaměstnanec" in zp.note

    fc = overview.forecast(o)
    assert fc.expense_run_rate == Decimal(30_000) / 9
    assert fc.tax.expenses == Decimal(40_000) + fc.expense_run_rate * 3


def test_employment_income_raises_tax_without_second_credit(ucetka_home: Path) -> None:
    _actual_profile(ucetka_home, "employment_income: 600000\n")
    _issue("2602001", date(2026, 2, 10), "100000")
    o = overview.build(2026, date(2026, 10, 4), sync=False)
    assert o.tax.tax == Decimal(15_000)


def test_unsupported_profiles_fail_fast(ucetka_home: Path) -> None:
    import pytest
    from pydantic import ValidationError

    from ucetka.payments import load_settings

    for bad in ("health_status: none\n", "side_activity: false\n"):
        (ucetka_home / "tax.yaml").write_text(bad)
        with pytest.raises(ValidationError, match="zatím"):
            load_settings()
