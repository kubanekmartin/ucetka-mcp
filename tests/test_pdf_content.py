"""Asserty na § 29 ZoDPH náležitosti přítomné v textu PDF."""
from io import BytesIO
from pathlib import Path

import pdfplumber
import pytest
import yaml

from ucetka.config import load_supplier
from ucetka.isdoc.embed import embed_isdoc
from ucetka.isdoc.generator import render_isdoc
from ucetka.models import Invoice
from ucetka.render.pdf import render_pdf
from ucetka.render.qr import make_qr_data_uri

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"


def _build_pdf() -> bytes:
    supplier = load_supplier(EXAMPLES / "supplier.yaml").party()
    data = yaml.safe_load((EXAMPLES / "2605001.yaml").read_text())
    data["supplier"] = supplier.model_dump()
    inv = Invoice.model_validate(data)
    pdf = render_pdf(inv, qr_data_uri=make_qr_data_uri(inv), has_isdoc=True)
    return embed_isdoc(pdf, render_isdoc(inv))


@pytest.fixture(scope="module")
def pdf_text() -> str:
    with pdfplumber.open(BytesIO(_build_pdf())) as pdf:
        raw = "\n".join(page.extract_text() or "" for page in pdf.pages)
    return raw.replace(" ", " ")  # NBSP → běžná mezera pro snadnější hledání


@pytest.mark.parametrize(
    "expected",
    [
        "FAKTURA",
        "DAŇOVÝ DOKLAD",
        "#2605001",
        "IBAN",
        "Petr Svoboda",
        "98765434",
        "CZ98765434",
        "Plátce DPH",
        "Hlavní 123",
        "Kolín",
        "Vzorová firma s.r.o.",
        "87654326",
        "CZ87654326",
        "Vzorová 1234/5",
        "19-2000145399/0100",
        "Převodem",
        "15. 05. 2026",
        "29. 05. 2026",
        "přednáška o umělé inteligenci",
        "5 000,00",
        "1 050,00",
        "6 050,00",
        "21",
        "CELKEM K ÚHRADĚ",
        "Vystavil(a)",
    ],
)
def test_pdf_contains(pdf_text: str, expected: str) -> None:
    assert expected in pdf_text, f"Chybí v PDF: {expected!r}"


def test_pdf_does_not_contain_placeholder(pdf_text: str) -> None:
    assert "{{" not in pdf_text


def test_english_invoice_has_english_labels() -> None:
    supplier = load_supplier(EXAMPLES / "supplier.yaml").party()
    data = yaml.safe_load((EXAMPLES / "2605001.yaml").read_text())
    data["supplier"] = supplier.model_dump()
    inv = Invoice.model_validate(data | {"language": "en"})
    with pdfplumber.open(BytesIO(render_pdf(inv, qr_data_uri=make_qr_data_uri(inv), qr_label="QR Platba"))) as pdf:
        text = "\n".join(page.extract_text() or "" for page in pdf.pages).replace(" ", " ")
    for expected in ["INVOICE", "TAX DOCUMENT", "#2605001", "Company ID", "VAT registered", "15 May 2026",
                     "Bank transfer", "Czech Republic", "1 pcs", "Taxable supply", "Total due", "6,050.00"]:
        assert expected.casefold() in text.casefold(), expected
    for czech in ["Odběratel", "Převodem", "Splatnost", "Celkem k úhradě", "Česká republika"]:
        assert czech.casefold() not in text.casefold(), czech
