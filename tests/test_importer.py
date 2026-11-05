from datetime import date
from pathlib import Path

import pytest
import yaml

from ucetka import ledger
from ucetka.config import load_supplier
from ucetka.importer import ImportFailed, load_issues, parse_isdoc, sync
from ucetka.isdoc.generator import render_isdoc
from ucetka.issue import render_invoice_pdf
from ucetka.models import Invoice

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"


def _invoice(number: str = "2610001") -> Invoice:
    data = yaml.safe_load((EXAMPLES / "2605001.yaml").read_text())
    data["supplier"] = load_supplier(EXAMPLES / "supplier.yaml").party().model_dump()
    d = date(2026, 10, 1)
    data.update(number=number, issue_date=d, tax_point_date=d, due_date=d)
    data["payment"]["variable_symbol"] = number
    return Invoice.model_validate(data)


def _write_pdf(folder: Path, inv: Invoice) -> Path:
    path = folder / "26_10_vydane" / f"faktura_{inv.number}.pdf"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(render_invoice_pdf(inv, pdf_a3=False))
    return path


def test_parse_roundtrip_of_own_isdoc() -> None:
    inv = _invoice()
    parsed = parse_isdoc(render_isdoc(inv))
    assert parsed.number == inv.number
    assert parsed.total_net == inv.total_net
    assert parsed.total_vat == inv.total_vat
    assert parsed.customer.ico == inv.customer.ico


def test_note_survives_roundtrip() -> None:
    inv = _invoice().model_copy(update={"note": "OBJEDNÁVKA č. 12345678/100"})
    assert parse_isdoc(render_isdoc(inv)).note == "OBJEDNÁVKA č. 12345678/100"


def test_legal_note_is_not_imported_as_user_note() -> None:
    data = yaml.safe_load((EXAMPLES / "eu_customer.yaml").read_text())
    data["supplier"] = load_supplier(EXAMPLES / "supplier.yaml").party().model_dump()
    inv = Invoice.model_validate(data | {"note": "PO 42"})
    parsed = parse_isdoc(render_isdoc(inv))
    assert parsed.note == "PO 42"
    assert parsed.legal_notes == inv.legal_notes


def test_parse_isdoc_5_namespace() -> None:
    xml = render_isdoc(_invoice()).replace(b"http://isdoc.cz/namespace/2013", b"http://isdoc.cz/namespace/invoice")
    assert parse_isdoc(xml).number == "2610001"


def test_missing_bank_account_is_allowed_for_import() -> None:
    xml = render_isdoc(_invoice()).replace(b"<ID>19-2000145399</ID>", b"<ID/>")
    assert parse_isdoc(xml).payment.bank_account is None


def test_totals_mismatch_is_flagged() -> None:
    xml = render_isdoc(_invoice()).replace(
        b"<TaxExclusiveAmount>5000.00</TaxExclusiveAmount>", b"<TaxExclusiveAmount>4000.00</TaxExclusiveAmount>"
    )
    with pytest.raises(ImportFailed, match="Základ nesedí"):
        parse_isdoc(xml)


def test_missing_customer_is_flagged() -> None:
    xml = render_isdoc(_invoice()).replace(b"<Name>Vzorov\xc3\xa1 firma s.r.o.</Name>", b"<Name/>")
    with pytest.raises(ImportFailed, match="odběratel"):
        parse_isdoc(xml)


def test_credit_note_is_flagged() -> None:
    xml = render_isdoc(_invoice()).replace(b"<DocumentType>1</DocumentType>", b"<DocumentType>2</DocumentType>")
    with pytest.raises(ImportFailed, match="Typ dokladu 2"):
        parse_isdoc(xml)


def test_sync_imports_new_pdfs_and_flags_unreadable(tmp_path: Path) -> None:
    folder = tmp_path / "vydane"
    _write_pdf(folder, _invoice("2610001"))
    broken = folder / "26_10_vydane" / "faktura_2610002.pdf"
    broken.write_bytes(render_invoice_pdf(_invoice("2610002"), isdoc=False, pdf_a3=False))
    old = folder / "22_vydane" / "faktura_220001.pdf"
    old.parent.mkdir(parents=True)
    old.write_bytes(b"not a pdf")

    result = sync(folder, since_year=2025)
    assert result.imported == ["2610001"]
    assert [i.pdf_path.name for i in result.issues] == ["faktura_2610002.pdf"]
    assert "ISDOC" in result.issues[0].reason
    assert ledger.get("2610001").source == "imported"
    assert load_issues()[0].pdf_path == broken

    again = sync(folder, since_year=2025)
    assert again.imported == []
    assert ledger.next_number(date(2026, 10, 5)) == "2610002"


def test_moved_pdf_is_relinked_and_deleted_is_flagged(tmp_path: Path) -> None:
    folder = tmp_path / "vydane"
    original = _write_pdf(folder, _invoice("2610001"))
    sync(folder, since_year=2025)

    moved = folder / "26_10_jinde" / original.name
    moved.parent.mkdir()
    original.rename(moved)
    result = sync(folder, since_year=2025)
    assert result.issues == []
    assert ledger.get("2610001").pdf_path == moved

    moved.unlink()
    result = sync(folder, since_year=2025)
    assert "chybí" in result.issues[0].reason
    assert ledger.exists("2610001")
