from io import BytesIO
from pathlib import Path

import pikepdf
import pytest
import yaml

from ucetka.config import load_supplier
from ucetka.isdoc.embed import embed_isdoc
from ucetka.isdoc.generator import render_isdoc
from ucetka.models import Invoice
from ucetka.render.pdf import render_pdf

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"


def _build_pdfa(tmp_path: Path) -> bytes:
    supplier = load_supplier(EXAMPLES / "supplier.yaml").party()
    data = yaml.safe_load((EXAMPLES / "2605001.yaml").read_text())
    data["supplier"] = supplier.model_dump()
    inv = Invoice.model_validate(data)
    pdf = render_pdf(inv, pdf_a3=True, has_isdoc=True)
    return embed_isdoc(pdf, render_isdoc(inv))


@pytest.fixture(scope="module")
def pdfa_bytes(tmp_path_factory) -> bytes:
    return _build_pdfa(tmp_path_factory.mktemp("pdfa"))


def test_pdfa3_claim_in_xmp(pdfa_bytes: bytes) -> None:
    pdf = pikepdf.Pdf.open(BytesIO(pdfa_bytes))
    meta = pdf.open_metadata()
    assert meta["pdfaid:part"] == "3"
    assert meta["pdfaid:conformance"] == "A"


def test_pdfa3_has_output_intent(pdfa_bytes: bytes) -> None:
    pdf = pikepdf.Pdf.open(BytesIO(pdfa_bytes))
    intents = pdf.Root.get("/OutputIntents")
    assert intents is not None
    assert len(intents) > 0
    intent = intents[0]
    assert "/DestOutputProfile" in intent


def test_pdfa3_associated_files_entry(pdfa_bytes: bytes) -> None:
    pdf = pikepdf.Pdf.open(BytesIO(pdfa_bytes))
    af = pdf.Root.get("/AF")
    assert af is not None
    assert len(af) >= 1
    spec = af[0]
    assert str(spec.get("/AFRelationship")) == "/Alternative"


def test_pdfa3_isdoc_attachment_preserved(pdfa_bytes: bytes) -> None:
    pdf = pikepdf.Pdf.open(BytesIO(pdfa_bytes))
    assert "invoice.isdoc" in pdf.attachments
