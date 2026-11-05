import csv
from datetime import date
from decimal import Decimal
from pathlib import Path

import pdfplumber
import yaml

from ucetka import expenses, ledger
from ucetka.config import load_supplier
from ucetka.export import kniha
from ucetka.issue import build_credit_note, issue
from ucetka.models import Invoice

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"


def _invoice(number: str, d: date, price: str) -> Invoice:
    data = yaml.safe_load((EXAMPLES / "2605001.yaml").read_text())
    data["supplier"] = load_supplier(EXAMPLES / "supplier.yaml").party().model_dump()
    data.update(number=number, issue_date=d, tax_point_date=d, due_date=d)
    data["lines"][0]["unit_price_net"] = price
    return issue(Invoice.model_validate(data)).invoice


def _setup() -> None:
    _invoice("2603001", date(2026, 3, 10), "10000")
    ledger.mark_paid("2603001", date(2026, 3, 20))
    issue(build_credit_note("2603001", "Sleva", issue_date=date(2026, 4, 1),
                            lines=[_invoice("2603002", date(2026, 3, 11), "1000").lines[0]]))
    ledger.mark_paid("2604001", date(2026, 4, 2))
    common = dict(supplier_name="Alza.cz a.s.", supplier_dic="CZ27082440", issue_date=date(2026, 3, 1),
                  tax_point_date=date(2026, 3, 1),
                  vat_rows=[expenses.VatRow(rate=Decimal(21), base=Decimal(2000), vat=Decimal(420))], total=Decimal(2420))
    expenses.save(expenses.Expense(id="v1", document_number="A1", paid_date=date(2026, 3, 2), note="Monitor", **common))
    expenses.save(expenses.Expense(id="v2", document_number="A2", paid_date=date(2026, 3, 3), tax_deductible=False, **common))
    expenses.save(expenses.Expense(id="v3", document_number="A3", **common))


def test_book_totals_and_credit_note() -> None:
    _setup()
    book = kniha.build(2026)
    assert [r.document for r in book.rows] == ["A1", "A2", "2603001", "2604001"]
    assert book.income == Decimal(9000)
    assert book.deductible_expenses == Decimal(2000)
    assert book.non_deductible_expenses == Decimal(2420)
    assert book.difference == Decimal(7000)


def test_csv_and_pdf() -> None:
    _setup()
    book = kniha.build(2026)
    csv_path = kniha.write_csv(book)
    with csv_path.open(encoding="utf-8-sig") as f:
        rows = list(csv.reader(f, delimiter=";"))
    assert rows[0] == kniha.HEADER
    assert rows[1][:5] == ["2026-03-02", "A1", "Alza.cz a.s.", "Monitor", "výdaj"]
    with pdfplumber.open(kniha.write_pdf(book)) as pdf:
        text = pdf.pages[0].extract_text()
    assert "Kniha příjmů a výdajů 2026" in text
    assert "7 000,00 Kč" in text
