from decimal import Decimal
from pathlib import Path

import pytest
import yaml
from lxml import etree

from ucetka.config import load_supplier
from ucetka.export.pohoda import render_pohoda_xml
from ucetka.models import Invoice

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"

NS = {
    "dat": "http://www.stormware.cz/schema/version_2/data.xsd",
    "inv": "http://www.stormware.cz/schema/version_2/invoice.xsd",
    "typ": "http://www.stormware.cz/schema/version_2/type.xsd",
}


def _load_invoice() -> Invoice:
    supplier = load_supplier(EXAMPLES / "supplier.yaml").party()
    data = yaml.safe_load((EXAMPLES / "2605001.yaml").read_text())
    data["supplier"] = supplier.model_dump()
    return Invoice.model_validate(data)


def test_pohoda_xml_encoding_is_windows_1250() -> None:
    xml = render_pohoda_xml(_load_invoice())
    # Pohoda historicky vyžaduje windows-1250, ne UTF-8.
    assert b'encoding="windows-1250"' in xml[:80]
    # Diakritika musí dekódovat bez ztrát
    decoded = xml.decode("windows-1250")
    assert "Petr Svoboda" in decoded
    assert "přednáška" in decoded
    assert "Vzorová firma s.r.o." in decoded


def test_pohoda_xml_structure() -> None:
    xml = render_pohoda_xml(_load_invoice())
    tree = etree.fromstring(xml)
    assert tree.tag == "{http://www.stormware.cz/schema/version_2/data.xsd}dataPack"
    assert tree.get("ico") == "98765434"

    inv = tree.find("dat:dataPackItem/inv:invoice", namespaces=NS)
    assert inv is not None

    header = inv.find("inv:invoiceHeader", namespaces=NS)
    assert header.findtext("inv:invoiceType", namespaces=NS) == "issuedInvoice"
    assert header.findtext("inv:number/typ:numberRequested", namespaces=NS) == "2605001"
    assert header.findtext("inv:symVar", namespaces=NS) == "2605001"
    assert header.findtext("inv:date", namespaces=NS) == "2026-05-15"
    assert header.findtext("inv:dateDue", namespaces=NS) == "2026-05-29"

    summary = inv.find("inv:invoiceSummary/inv:homeCurrency", namespaces=NS)
    assert summary.findtext("typ:priceHigh", namespaces=NS) == "5000.00"
    assert summary.findtext("typ:priceHighVAT", namespaces=NS) == "1050.00"
    assert summary.findtext("typ:priceHighSum", namespaces=NS) == "6050.00"


def test_pohoda_invoice_item_uses_rate_high_for_21() -> None:
    xml = render_pohoda_xml(_load_invoice())
    tree = etree.fromstring(xml)
    item = tree.find(".//inv:invoiceItem", namespaces=NS)
    assert item.findtext("inv:rateVAT", namespaces=NS) == "high"
    assert item.findtext("inv:homeCurrency/typ:unitPrice", namespaces=NS) == "5000.00"


def _with_line(**changes) -> Invoice:
    inv = _load_invoice()
    return inv.model_copy(update={"lines": [inv.lines[0].model_copy(update=changes)]})


def test_pohoda_xml_passes_official_xsd() -> None:
    from ucetka.export.validator import validate

    validate(render_pohoda_xml(_load_invoice()))


def test_pohoda_myidentity_has_no_country() -> None:
    tree = etree.fromstring(render_pohoda_xml(_load_invoice()))
    assert tree.find(".//inv:myIdentity//typ:country", NS) is None


def test_pohoda_classification_vat_inland_for_payer() -> None:
    tree = etree.fromstring(render_pohoda_xml(_load_invoice()))
    assert tree.findtext(".//inv:classificationVAT/typ:classificationVATType", namespaces=NS) == "inland"


def test_pohoda_rejects_item_text_over_90_chars() -> None:
    with pytest.raises(ValueError, match="max. 90"):
        render_pohoda_xml(_with_line(description="x" * 91))


def test_pohoda_rejects_char_outside_cp1250() -> None:
    with pytest.raises(ValueError, match="windows-1250"):
        render_pohoda_xml(_with_line(description="Nasazení → produkce"))


def test_pohoda_rejects_unknown_vat_rate() -> None:
    with pytest.raises(ValueError, match="15"):
        render_pohoda_xml(_with_line(vat_rate=Decimal(15)))
