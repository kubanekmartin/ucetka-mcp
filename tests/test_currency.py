from datetime import date
from decimal import Decimal
from io import BytesIO
from pathlib import Path

import httpx
import pdfplumber
import pytest
import yaml
from lxml import etree
from pydantic import ValidationError

from ucetka.cnb import fetch_rate
from ucetka.config import load_supplier
from ucetka.export.pohoda import render_pohoda_xml
from ucetka.isdoc.generator import render_isdoc
from ucetka.isdoc.validator import validate
from ucetka.models import Invoice
from ucetka.render.pdf import render_pdf
from ucetka.render.qr import make_spayd
from ucetka.render.sind import make_sind

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"
NS = {"i": "http://isdoc.cz/namespace/2013"}


def _eur_rc() -> Invoice:
    data = yaml.safe_load((EXAMPLES / "eur.yaml").read_text())
    data["supplier"] = load_supplier(EXAMPLES / "supplier.yaml").party().model_dump()
    return Invoice.model_validate(data)


def _eur_domestic_with_vat() -> Invoice:
    """Plátce fakturuje českému odběrateli v EUR s 21 % DPH — DPH musí být i v Kč."""
    data = yaml.safe_load((EXAMPLES / "2605001.yaml").read_text())
    data["supplier"] = load_supplier(EXAMPLES / "supplier.yaml").party().model_dump()
    data.update(currency="EUR", exchange_rate="24.465")
    data["lines"][0]["unit_price_net"] = "200"
    return Invoice.model_validate(data)


def test_foreign_currency_requires_rate() -> None:
    data = yaml.safe_load((EXAMPLES / "eur.yaml").read_text())
    data["supplier"] = load_supplier(EXAMPLES / "supplier.yaml").party().model_dump()
    data.pop("exchange_rate")
    with pytest.raises(ValidationError, match="exchange_rate"):
        Invoice.model_validate(data)


def test_vat_in_czk_computed_from_converted_base() -> None:
    inv = _eur_domestic_with_vat()
    assert inv.total_gross == Decimal("242.00")
    assert inv.total_net_czk == Decimal("4893.00")
    assert inv.total_vat_czk == Decimal("1027.53")


def test_isdoc_foreign_currency_valid_with_curr_pairs() -> None:
    for inv in (_eur_rc(), _eur_domestic_with_vat()):
        xml = render_isdoc(inv)
        validate(xml)
    doc = etree.fromstring(render_isdoc(_eur_domestic_with_vat()))
    assert doc.findtext("i:ForeignCurrencyCode", namespaces=NS) == "EUR"
    assert doc.findtext("i:LocalCurrencyCode", namespaces=NS) == "CZK"
    assert doc.findtext("i:CurrRate", namespaces=NS) == "24.465"
    assert doc.findtext("i:TaxTotal/i:TaxAmount", namespaces=NS) == "1027.53"
    assert doc.findtext("i:TaxTotal/i:TaxAmountCurr", namespaces=NS) == "42.00"
    assert doc.findtext("i:LegalMonetaryTotal/i:PayableAmountCurr", namespaces=NS) == "242.00"


def test_pdf_shows_rate_and_czk_recap() -> None:
    with pdfplumber.open(BytesIO(render_pdf(_eur_domestic_with_vat()))) as doc:
        text = doc.pages[0].extract_text()
    assert "1 EUR = 24,465 Kč" in text
    assert "1 027,53" in text
    assert "242,00" in text
    assert "€" in text


def test_sind_uses_czk_bases_and_fx() -> None:
    sind = make_sind(_eur_domestic_with_vat())
    assert "*AM:242.00*" in sind
    assert "*TB0:4893.00*T0:1027.53*CC:EUR*FX:24.465*FXA:1*" in sind


def test_spayd_in_eur() -> None:
    spayd = make_spayd(_eur_rc())
    assert "*AM:400.00*" in spayd
    assert "*CC:EUR*" in spayd


def test_pohoda_foreign_currency_valid() -> None:
    xml = render_pohoda_xml(_eur_rc(), eu_classification="XYZ")
    assert b"<typ:ids>EUR</typ:ids>" in xml
    assert b"<typ:rate>24.465</typ:rate>" in xml
    xml = render_pohoda_xml(_eur_domestic_with_vat())
    assert b"<typ:priceHighVAT>1027.53</typ:priceHighVAT>" in xml


def test_fetch_rate_parses_cnb_response() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["date"] == "2026-10-03"
        return httpx.Response(200, json={"rates": [
            {"currencyCode": "EUR", "rate": 24.465, "amount": 1},
            {"currencyCode": "HUF", "rate": 6.626, "amount": 100},
        ]})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    assert fetch_rate("HUF", date(2026, 10, 3), client=client) == (Decimal("6.626"), 100)
    with pytest.raises(ValueError, match="XYZ"):
        fetch_rate("XYZ", date(2026, 10, 3), client=client)
