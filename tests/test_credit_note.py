from datetime import date
from decimal import Decimal
from io import BytesIO
from pathlib import Path

import pdfplumber
import pikepdf
import pytest
import yaml
from lxml import etree
from pydantic import ValidationError

from ucetka import ledger, overview
from ucetka.config import load_supplier
from ucetka.export import dph
from ucetka.importer import ImportFailed, parse_isdoc
from ucetka.issue import build_credit_note, issue
from ucetka.models import Invoice, LineItem

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"
NS = {"i": "http://isdoc.cz/namespace/2013"}


def _issue_original(net: str = "20000") -> Invoice:
    data = yaml.safe_load((EXAMPLES / "2605001.yaml").read_text())
    data["supplier"] = load_supplier(EXAMPLES / "supplier.yaml").party().model_dump()
    data["number"] = "2609001"
    data |= {
        "issue_date": date(2026, 9, 30),
        "tax_point_date": date(2026, 9, 30),
        "due_date": date(2026, 10, 14),
    }
    data["lines"][0]["unit_price_net"] = net
    return issue(Invoice.model_validate(data)).invoice


def _isdoc(pdf: Path) -> etree._Element:
    with pikepdf.open(pdf) as p:
        return etree.fromstring(p.attachments["invoice.isdoc"].get_file().read_bytes())


def test_full_credit_note_pdf_and_isdoc() -> None:
    _issue_original()
    record = issue(
        build_credit_note(
            "2609001", "Chybně fakturovaná přednáška", issue_date=date(2026, 10, 8)
        )
    )
    assert record.invoice.number == "2610001"
    assert record.pdf_path.name == "dobropis_2610001.pdf"

    doc = _isdoc(record.pdf_path)
    assert doc.findtext("i:DocumentType", namespaces=NS) == "2"
    assert (
        doc.findtext(
            "i:OriginalDocumentReferences/i:OriginalDocumentReference/i:ID",
            namespaces=NS,
        )
        == "2609001"
    )
    assert (
        doc.findtext("i:LegalMonetaryTotal/i:PayableAmount", namespaces=NS)
        == "24200.00"
    )
    assert "Důvod opravy: Chybně fakturovaná přednáška" in doc.findtext(
        "i:Note", namespaces=NS
    )

    with pdfplumber.open(BytesIO(record.pdf_path.read_bytes())) as pdf:
        text = pdf.pages[0].extract_text()
        assert len(pdf.pages) == 1
        assert not pdf.pages[0].images
    assert "OPRAVNÝ DAŇOVÝ DOKLAD — DOBROPIS" in text
    assert "OPRAVUJE DOKLAD 2609001 ze dne 30. 09. 2026" in text
    assert "DŮVOD OPRAVY Chybně fakturovaná přednáška" in text
    assert "-20 000,00" in text
    assert "CELKEM K VRÁCENÍ" in text


def test_partial_credit_note_and_limit() -> None:
    _issue_original()
    line = LineItem(
        quantity=Decimal(1),
        unit="ks",
        description="Sleva",
        unit_price_net=Decimal(15000),
        vat_rate=Decimal(21),
    )
    issue(
        build_credit_note(
            "2609001", "Sleva", lines=[line], issue_date=date(2026, 10, 8)
        )
    )
    with pytest.raises(ValueError, match="přesáhly"):
        build_credit_note(
            "2609001", "Další sleva", lines=[line], issue_date=date(2026, 10, 9)
        )


def test_credit_note_requires_original_and_reason() -> None:
    original = _issue_original()
    with pytest.raises(ValidationError, match="Dobropis"):
        original.model_copy(update={"document_type": "credit_note"}).model_validate(
            original.model_dump() | {"document_type": "credit_note"}
        )


def test_credit_note_reduces_overview_and_vat_return(ucetka_home: Path) -> None:
    (ucetka_home / "tax.yaml").write_text(
        yaml.safe_dump(
            {
                "epo": {
                    "c_ufo": "457",
                    "c_pracufo": "2607",
                    "dic": "98765434",
                    "jmeno": "M",
                    "prijmeni": "K",
                    "zkrobchjm": "M K",
                    "ulice": "U",
                    "c_pop": "1",
                    "naz_obce": "O",
                    "psc": "51251",
                }
            }
        )
    )
    _issue_original()
    issue(build_credit_note("2609001", "Storno zakázky", issue_date=date(2026, 10, 8)))

    o = overview.build(2026, date(2026, 10, 20), sync=False)
    assert o.months[8].issued_net == Decimal("20000.00")
    assert o.months[9].issued_net == Decimal("-20000.00")

    ret = dph.collect(2026, 4)
    lines = dph.dp3_lines(ret)
    assert (lines["Veta1"]["obrat23"], lines["Veta1"]["dan23"]) == (-20000, -4200)
    assert ret.a4[0].doc_number == "2610001"
    kh = etree.fromstring(dph.render_kh(ret, date(2026, 1, 20)))
    assert kh.find("DPHKH1/VetaA4").get("zakl_dane1") == "-20000"


def test_small_original_credit_goes_to_a5(ucetka_home: Path) -> None:
    (ucetka_home / "tax.yaml").write_text("vat_period: quarterly\n")
    _issue_original(net="5000")
    issue(build_credit_note("2609001", "Sleva", issue_date=date(2026, 10, 8)))
    ret = dph.collect(2026, 4)
    assert ret.a4 == []
    assert ret.a5[Decimal(21)] == [Decimal("-5000.00"), Decimal("-1050.00")]


def test_foreign_credit_note_from_other_systems_is_flagged() -> None:
    original = _issue_original()
    from ucetka.isdoc.generator import render_isdoc

    note = build_credit_note("2609001", "x", issue_date=date(2026, 10, 8))
    with pytest.raises(ImportFailed, match="Typ dokladu 2"):
        parse_isdoc(render_isdoc(note))
    assert ledger.get(original.number).invoice.number == "2609001"


def test_unpaid_credit_note_is_not_overdue_in_ui() -> None:
    from fastapi.testclient import TestClient
    from ucetka.web.app import app

    _issue_original()
    issue(build_credit_note("2609001", "Sleva", issue_date=date(2026, 10, 1)))
    ledger.mark_paid("2609001", date(2026, 10, 2))
    html = TestClient(app).get("/faktury").text
    assert "Dobropis k vrácení" in html
    assert "Po splatnosti 1" not in html and "0 po splatnosti" in html
