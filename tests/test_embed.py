from pathlib import Path

import pikepdf
import pytest
import yaml

from ucetka.config import load_supplier
from ucetka.isdoc.embed import embed_isdoc, extract_attachment
from ucetka.isdoc.generator import render_isdoc
from ucetka.isdoc.validator import validate
from ucetka.models import Invoice
from ucetka.render.pdf import render_pdf

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"


def _load_invoice() -> Invoice:
    supplier = load_supplier(EXAMPLES / "supplier.yaml").party()
    data = yaml.safe_load((EXAMPLES / "2605001.yaml").read_text())
    data["supplier"] = supplier.model_dump()
    return Invoice.model_validate(data)


@pytest.fixture(scope="module")
def isdoc_pdf() -> tuple[bytes, bytes]:
    inv = _load_invoice()
    pdf = render_pdf(inv, has_isdoc=True)
    isdoc = render_isdoc(inv)
    return embed_isdoc(pdf, isdoc), isdoc


def test_embed_roundtrip(isdoc_pdf: tuple[bytes, bytes]) -> None:
    pdf, isdoc = isdoc_pdf
    extracted = extract_attachment(pdf, "invoice.isdoc")
    assert extracted == isdoc


def test_extracted_isdoc_validates(isdoc_pdf: tuple[bytes, bytes]) -> None:
    pdf, _ = isdoc_pdf
    extracted = extract_attachment(pdf, "invoice.isdoc")
    validate(extracted)


def test_attachment_has_correct_relationship(isdoc_pdf: tuple[bytes, bytes]) -> None:
    pdf_bytes, _ = isdoc_pdf
    from io import BytesIO

    pdf = pikepdf.Pdf.open(BytesIO(pdf_bytes))
    fs = pdf.attachments["invoice.isdoc"]
    assert str(fs.relationship) == "/Alternative"


def test_attachment_named_per_isdoc_spec(isdoc_pdf: tuple[bytes, bytes]) -> None:
    from io import BytesIO

    pdf = pikepdf.Pdf.open(BytesIO(isdoc_pdf[0]))
    spec = pdf.Root.AF[0]
    assert str(spec.F) == "invoice.isdoc"
    assert str(spec.UF) == "invoice.isdoc"
