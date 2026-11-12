"""MCP-layer tests for sync_invoices, add_expense, mark_expense_paid, export_book, add_manual_invoice and
issue_credit_note."""

import csv
import os
from datetime import date
from decimal import Decimal
from pathlib import Path

import pikepdf
import pytest
import yaml
from pydantic import ValidationError

from ucetka import expenses, importer, ledger
from ucetka.config import load_supplier
from ucetka.customers import get_customer
from ucetka.isdoc.generator import render_isdoc
from ucetka.issue import render_invoice_pdf
from ucetka.mcp_server import (
    LineInput,
    add_expense,
    add_manual_invoice,
    export_book,
    issue_credit_note,
    issue_invoice,
    list_import_issues,
    mark_expense_paid,
    mark_paid,
    sync_invoices,
)
from ucetka.models import Invoice

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"


@pytest.fixture(autouse=True)
def _pin_sync_year(monkeypatch: pytest.MonkeyPatch) -> None:
    # Fixtures use 2026 dates and 26_* folders; sync() otherwise skips folders older than last year,
    # which would make these tests start failing in 2028.
    for module in (importer, expenses):
        original = module.sync
        monkeypatch.setattr(module, "sync", lambda folder=None, *, since_year=2026, _f=original: _f(folder, since_year=since_year))


def _issued_invoice(number: str) -> Invoice:
    data = yaml.safe_load((EXAMPLES / "2605001.yaml").read_text())
    data["supplier"] = load_supplier(EXAMPLES / "supplier.yaml").party().model_dump()
    d = date(2026, 10, 1)
    data.update(number=number, issue_date=d, tax_point_date=d, due_date=d)
    data["payment"]["variable_symbol"] = number
    return Invoice.model_validate(data)


def _drop_issued_pdf(number: str) -> Path:
    path = Path(os.environ["UCETKA_OUT"]) / "26_10_vydane" / f"faktura_{number}.pdf"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(render_invoice_pdf(_issued_invoice(number), pdf_a3=False))
    return path


def _drop_received_isdoc(expense_id: str) -> Path:
    """A received invoice: ISDOC sidecar where the example customer is the supplier and we are the customer."""
    inv = _issued_invoice(expense_id).model_copy(
        update={"supplier": get_customer("vzorova-firma"), "customer": load_supplier().party()}
    )
    path = Path(os.environ["UCETKA_RECEIVED"]) / "26_10_prijate" / f"{expense_id}.isdoc"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(render_isdoc(inv))
    return path


def _blank_pdf(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    pdf = pikepdf.new()
    pdf.add_blank_page()
    pdf.save(path)
    return path


def _expense(**kw) -> str:
    args = {
        "expense_id": "2610001",
        "supplier_name": "Alza.cz a.s.",
        "document_number": "FV-1",
        "tax_point_date": "2026-10-05",
        "base": Decimal(1000),
    }
    return add_expense(**(args | kw))


# --- sync_invoices -------------------------------------------------------------------------------------------


def test_sync_invoices_imports_issued_and_received_and_is_idempotent() -> None:
    _drop_issued_pdf("2610555")
    _drop_received_isdoc("2610777")

    out = sync_invoices()
    assert out.startswith("Vydané: +1 · přijaté: +1 · k zadání: 0")
    assert ledger.get("2610555").invoice.total_net == Decimal("5000.00")
    assert ledger.get("2610555").source == "imported"
    exp = expenses.list_expenses()[0]
    assert (exp.id, exp.supplier_name, exp.base_total) == ("2610777", "Vzorová firma s.r.o.", Decimal("5000.00"))

    assert sync_invoices().startswith("Vydané: +0 · přijaté: +0 · k zadání: 0")
    assert len(ledger.list_records()) == 1 and len(expenses.list_expenses()) == 1
    assert list_import_issues() == "Nic, vše načteno."


def test_sync_invoices_flags_unreadable_pdfs() -> None:
    issued = _blank_pdf(Path(os.environ["UCETKA_OUT"]) / "26_10_vydane" / "faktura_2610999.pdf")
    received = _blank_pdf(Path(os.environ["UCETKA_RECEIVED"]) / "26_10_prijate" / "2610998.pdf")

    out = sync_invoices()
    assert "Vydané: +0 · přijaté: +0 · k zadání: 2" in out
    assert "list_import_issues" in out
    listing = list_import_issues()
    assert f"Vydaná {issued}" in listing
    assert f"Přijatá {received}" in listing
    assert ledger.list_records() == [] and expenses.list_expenses() == []


# --- add_expense / mark_expense_paid -------------------------------------------------------------------------


def test_add_expense_writes_record_with_computed_vat(ucetka_home: Path) -> None:
    out = _expense(note="Monitor")
    assert out.startswith("Zapsáno 2610001: Alza.cz a.s.")
    assert "vstup" in out
    assert (ucetka_home / "expenses" / "2610001.json").exists()
    e = expenses.list_expenses()[0]
    assert (e.base_total, e.vat_total, e.total) == (Decimal(1000), Decimal("210.00"), Decimal("1210.00"))
    assert e.paid_date is None and e.tax_point_date == date(2026, 10, 5) and e.note == "Monitor"


def test_add_expense_reverse_charge_and_explicit_vat() -> None:
    out = _expense(
        expense_id="2610002",
        supplier_name="Google Ireland Ltd",
        supplier_country="IE",
        supplier_dic="IE6388047V",
        reverse_charge=True,
        base=Decimal(500),
        vat=Decimal("105.00"),
        paid_date="2026-10-06",
    )
    assert "samovyměření" in out
    e = next(x for x in expenses.list_expenses() if x.id == "2610002")
    assert e.reverse_charge and e.total == Decimal(500) and e.vat_total == Decimal("105.00")
    assert e.paid_date == date(2026, 10, 6)


def test_add_expense_rejects_existing_id_and_keeps_original() -> None:
    _expense()
    with pytest.raises(ValueError, match="už v evidenci je"):
        _expense(supplier_name="Jiný dodavatel", base=Decimal(1))
    kept = expenses.list_expenses()
    assert len(kept) == 1 and kept[0].supplier_name == "Alza.cz a.s."


def test_add_expense_reverse_charge_requires_foreign_country() -> None:
    with pytest.raises(ValidationError):
        _expense(reverse_charge=True)
    assert expenses.list_expenses() == []


def test_add_expense_with_pdf_path_clears_import_issue() -> None:
    pdf = _blank_pdf(Path(os.environ["UCETKA_RECEIVED"]) / "26_10_prijate" / "2610003.pdf")
    sync_invoices()
    assert f"Přijatá {pdf}" in list_import_issues()
    _expense(expense_id="2610003", pdf_path=str(pdf))
    assert list_import_issues() == "Nic, vše načteno."
    assert expenses.list_expenses()[0].pdf_path == pdf


def test_mark_expense_paid_sets_date_and_rejects_second_call(ucetka_home: Path) -> None:
    _expense()
    out = mark_expense_paid("2610001", "2026-10-20")
    assert "Výdaj 2610001 (Alza.cz a.s.) zaplacen" in out and "20" in out
    assert expenses.list_expenses()[0].paid_date == date(2026, 10, 20)

    with pytest.raises(ValueError, match="už je zaplacený"):
        mark_expense_paid("2610001", "2026-10-25")
    assert expenses.list_expenses()[0].paid_date == date(2026, 10, 20)


def test_mark_expense_paid_defaults_to_today_and_unknown_fails() -> None:
    _expense()
    mark_expense_paid("2610001")
    assert expenses.list_expenses()[0].paid_date == date.today()
    with pytest.raises(FileNotFoundError):
        mark_expense_paid("9999999")


# --- export_book ---------------------------------------------------------------------------------------------


def test_export_book_writes_csv_and_pdf_for_paid_rows_only(tmp_path: Path) -> None:
    issue_invoice(lines=[LineInput(description="Přednáška", unit_price=Decimal(10000))],
                  customer_id="vzorova-firma", issue_date="2026-10-04")
    issue_invoice(lines=[LineInput(description="Nezaplaceno", unit_price=Decimal(777))],
                  customer_id="vzorova-firma", issue_date="2026-10-05")
    mark_paid(["2610001"], "2026-10-10")
    _expense(base=Decimal(2000), paid_date="2026-10-12")
    _expense(expense_id="2610002", base=Decimal(300))  # unpaid -> not in book

    out = export_book(2026)
    assert "Příjmy 10\xa0000 Kč" in out and "daňové výdaje 2\xa0000 Kč" in out and "rozdíl 8\xa0000 Kč" in out

    folder = tmp_path / "reports" / "2026"
    csv_path, pdf_path = folder / "kniha_2026.csv", folder / "kniha_2026.pdf"
    assert str(csv_path) in out and str(pdf_path) in out
    assert pdf_path.read_bytes().startswith(b"%PDF")
    with csv_path.open(encoding="utf-8-sig") as f:
        rows = list(csv.reader(f, delimiter=";"))
    docs = [r[1] for r in rows[1:] if r[1]]
    assert "2610001" in docs and "FV-1" in docs
    assert "2610002" not in docs  # the unpaid invoice and unpaid expense are excluded
    assert not any("777" in c for r in rows for c in r)

    assert export_book(2026) == out  # overwriting is repeatable


def test_export_book_empty_year_still_writes_files(tmp_path: Path) -> None:
    out = export_book(2024)
    assert "Příjmy 0 Kč" in out
    assert (tmp_path / "reports" / "2024" / "kniha_2024.csv").exists()
    assert (tmp_path / "reports" / "2024" / "kniha_2024.pdf").exists()


# --- add_manual_invoice --------------------------------------------------------------------------------------


def test_add_manual_invoice_records_ledger_without_rendering_pdf(tmp_path: Path, ucetka_home: Path) -> None:
    pdf = _blank_pdf(tmp_path / "out" / "26_10_vydane" / "faktura_2610042.pdf")
    before = pdf.read_bytes()
    out = add_manual_invoice(
        pdf_path=str(pdf),
        number="2610042",
        issue_date="2026-10-01",
        due_date="2026-10-15",
        customer_name="Ruční s.r.o.",
        lines=[LineInput(description="Služby", unit_price=Decimal(1000), quantity=Decimal(2))],
        customer_ico="12345678",
        customer_dic="CZ12345678",
        paid_date="2026-10-09",
    )
    assert out.startswith("Zapsáno 2610042: Ruční s.r.o.") and "základ 2\xa0000 Kč" in out and "DPH 420 Kč" in out
    assert (ucetka_home / "invoices" / "2610042.json").exists()
    rec = ledger.get("2610042")
    assert rec.source == "manual" and rec.pdf_path == pdf
    assert rec.status == "paid" and rec.paid_date == date(2026, 10, 9)
    assert rec.invoice.customer.vat_payer and rec.invoice.customer.ico == "12345678"
    assert rec.invoice.tax_point_date == date(2026, 10, 1)
    assert rec.invoice.total_vat == Decimal("420.00")
    assert pdf.read_bytes() == before  # PDF untouched
    assert sorted(p.name for p in pdf.parent.iterdir()) == ["faktura_2610042.pdf"]  # nothing generated


def test_add_manual_invoice_rejects_existing_number(tmp_path: Path) -> None:
    issue_invoice(lines=[LineInput(description="x", unit_price=Decimal(100))], customer_id="vzorova-firma",
                  issue_date="2026-10-04")
    with pytest.raises(ValueError, match="už v evidenci je"):
        add_manual_invoice(
            pdf_path=str(tmp_path / "x.pdf"), number="2610001", issue_date="2026-10-01", due_date="2026-10-15",
            customer_name="Dup", lines=[LineInput(description="y", unit_price=Decimal(1))],
        )
    assert ledger.get("2610001").invoice.customer.name != "Dup"


def test_add_manual_invoice_clears_import_issue(tmp_path: Path) -> None:
    pdf = _blank_pdf(Path(os.environ["UCETKA_OUT"]) / "26_10_vydane" / "faktura_2610043.pdf")
    sync_invoices()
    assert f"Vydaná {pdf}" in list_import_issues()
    add_manual_invoice(
        pdf_path=str(pdf), number="2610043", issue_date="2026-10-01", due_date="2026-10-15",
        customer_name="Ruční s.r.o.", lines=[LineInput(description="Služby", unit_price=Decimal(100))],
    )
    assert list_import_issues() == "Nic, vše načteno."
    assert ledger.get("2610043").status == "issued"


# --- issue_credit_note ---------------------------------------------------------------------------------------


def _original() -> None:
    issue_invoice(lines=[LineInput(description="Přednáška", unit_price=Decimal(10000))],
                  customer_id="vzorova-firma", issue_date="2026-10-04")


def test_issue_credit_note_full_creates_pdf_and_ledger_record() -> None:
    _original()
    out = issue_credit_note("2610001", "Chybná faktura", issue_date="2026-10-08")
    assert "Dobropis 2610002 k faktuře 2610001" in out and "dobropis_2610002.pdf" in out
    assert "základ 10\xa0000,00 Kč" in out and "DPH 2\xa0100,00 Kč" in out and "celkem 12\xa0100,00 Kč" in out
    rec = ledger.get("2610002")
    assert rec.pdf_path.exists() and rec.pdf_path.name == "dobropis_2610002.pdf"
    assert rec.pdf_path.read_bytes().startswith(b"%PDF")
    assert rec.invoice.is_credit_note and rec.invoice.original.number == "2610001"
    assert rec.invoice.correction_reason == "Chybná faktura"
    with pikepdf.open(rec.pdf_path) as p:
        assert "invoice.isdoc" in p.attachments
        assert b"<DocumentType>2</DocumentType>" in p.attachments["invoice.isdoc"].get_file().read_bytes()
    assert ledger.get("2610001").status == "issued"  # original untouched


def test_issue_credit_note_partial_and_cap() -> None:
    _original()
    out = issue_credit_note("2610001", "Sleva", lines=[LineInput(description="Sleva", unit_price=Decimal(3000))],
                            issue_date="2026-10-08")
    assert "základ 3\xa0000,00 Kč" in out and "DPH 630,00 Kč" in out
    with pytest.raises(ValueError):
        issue_credit_note("2610001", "Příliš", lines=[LineInput(description="Sleva", unit_price=Decimal(8000))],
                          issue_date="2026-10-09")
    assert len(ledger.list_records()) == 2  # no third document created


def test_issue_credit_note_unknown_cancelled_and_credit_note_originals_fail() -> None:
    with pytest.raises(ledger.InvoiceNotFound):
        issue_credit_note("2610001", "Neexistuje")
    _original()
    issue_credit_note("2610001", "Sleva", lines=[LineInput(description="S", unit_price=Decimal(100))],
                      issue_date="2026-10-08")
    with pytest.raises(ValueError, match="sám dobropis"):
        issue_credit_note("2610002", "Dobropis k dobropisu", issue_date="2026-10-09")
    from ucetka.mcp_server import cancel_invoice

    cancel_invoice("2610001", "storno")
    with pytest.raises(ValueError, match="stornovaná"):
        issue_credit_note("2610001", "Po stornu", issue_date="2026-10-09")
