from io import BytesIO
from pathlib import Path

import pdfplumber
import pytest
import yaml
from lxml import etree
from pydantic import ValidationError

from ucetka.customers import get_customer
from ucetka.isdoc.generator import render_isdoc
from ucetka.isdoc.validator import validate
from ucetka.models import Invoice
from ucetka.render.pdf import render_pdf

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"
NS = {"i": "http://isdoc.cz/namespace/2013"}


def _data() -> dict:
    data = yaml.safe_load((EXAMPLES / "neplatce.yaml").read_text())
    data["supplier"] = yaml.safe_load((EXAMPLES / "supplier_neplatce.yaml").read_text())
    data["customer"] = get_customer(data.pop("customer_id")).model_dump()
    return data


def test_non_payer_cannot_charge_vat() -> None:
    data = _data()
    data["lines"][0]["vat_rate"] = 21
    with pytest.raises(ValidationError, match="Neplátce"):
        Invoice.model_validate(data)


def test_non_payer_pdf_is_not_tax_document() -> None:
    pdf = render_pdf(Invoice.model_validate(_data()))
    with pdfplumber.open(BytesIO(pdf)) as doc:
        text = doc.pages[0].extract_text()
    assert "Nejsem plátce DPH." in text
    assert "DAŇOVÝ DOKLAD" not in text.upper()
    assert "DUZP" not in text
    assert "3 000,00" in text


def test_non_payer_isdoc_marks_vat_not_applicable() -> None:
    xml = render_isdoc(Invoice.model_validate(_data()))
    validate(xml)
    doc = etree.fromstring(xml)
    flags = doc.xpath("//i:VATApplicable/text()", namespaces=NS)
    assert flags and set(flags) == {"false"}


def test_non_payer_pohoda_export_is_valid_non_subsume() -> None:
    from ucetka.export.pohoda import render_pohoda_xml

    xml = render_pohoda_xml(Invoice.model_validate(_data()))
    assert b"<typ:classificationVATType>nonSubsume</typ:classificationVATType>" in xml
