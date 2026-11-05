from io import BytesIO
from pathlib import Path

import pdfplumber
import pytest
import yaml
from pydantic import ValidationError

from ucetka.config import load_supplier
from ucetka.export.pohoda import render_pohoda_xml
from ucetka.isdoc.generator import render_isdoc
from ucetka.isdoc.validator import validate
from ucetka.models import Invoice
from ucetka.render.pdf import render_pdf
from ucetka.render.sind import make_sind

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"


def _data() -> dict:
    data = yaml.safe_load((EXAMPLES / "eu_customer.yaml").read_text())
    data["supplier"] = load_supplier(EXAMPLES / "supplier.yaml").party().model_dump()
    return data


def test_reverse_charge_rejects_vat() -> None:
    data = _data()
    data["lines"][0]["vat_rate"] = 21
    with pytest.raises(ValidationError, match="daň odvede zákazník"):
        Invoice.model_validate(data)


def test_reverse_charge_rejects_czech_customer() -> None:
    data = _data()
    data["customer"]["dic"] = "CZ87654326"
    with pytest.raises(ValidationError, match="ne CZ"):
        Invoice.model_validate(data)


def test_reverse_charge_pdf_states_customer_pays_tax() -> None:
    pdf = render_pdf(Invoice.model_validate(_data()))
    with pdfplumber.open(BytesIO(pdf)) as doc:
        text = doc.pages[0].extract_text()
    assert "Daň odvede zákazník" in text
    assert "DAŇOVÝ DOKLAD" in text.upper()
    assert "DE123456789" in text


def test_reverse_charge_isdoc_valid_with_note() -> None:
    xml = render_isdoc(Invoice.model_validate(_data()))
    validate(xml)
    assert "Daň odvede zákazník".encode() in xml


def test_reverse_charge_sind_uses_ntb() -> None:
    assert "*NTB:10000.00*" in make_sind(Invoice.model_validate(_data()))


def test_reverse_charge_pohoda_requires_classification() -> None:
    inv = Invoice.model_validate(_data())
    with pytest.raises(ValueError, match="členění DPH"):
        render_pohoda_xml(inv)
    xml = render_pohoda_xml(inv, eu_classification="XYZ")
    assert b"<typ:ids>XYZ</typ:ids>" in xml
