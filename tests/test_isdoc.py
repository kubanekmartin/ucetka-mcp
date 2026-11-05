from pathlib import Path

import pytest
import yaml
from lxml import etree

from ucetka.config import load_supplier
from ucetka.isdoc.generator import deterministic_uuid, render_isdoc
from ucetka.isdoc.validator import IsdocValidationError, validate
from ucetka.models import Invoice

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"

NS = {"i": "http://isdoc.cz/namespace/2013"}


def _load_invoice() -> Invoice:
    supplier = load_supplier(EXAMPLES / "supplier.yaml").party()
    data = yaml.safe_load((EXAMPLES / "2605001.yaml").read_text())
    data["supplier"] = supplier.model_dump()
    return Invoice.model_validate(data)


def test_render_isdoc_passes_xsd() -> None:
    xml = render_isdoc(_load_invoice())
    validate(xml)


def test_render_isdoc_contains_key_fields() -> None:
    xml = render_isdoc(_load_invoice())
    tree = etree.fromstring(xml)
    assert tree.findtext("i:DocumentType", namespaces=NS) == "1"
    assert tree.findtext("i:ID", namespaces=NS) == "2605001"
    assert tree.findtext("i:LocalCurrencyCode", namespaces=NS) == "CZK"
    assert tree.findtext("i:LegalMonetaryTotal/i:PayableAmount", namespaces=NS) == "6050.00"
    assert (
        tree.findtext(
            "i:AccountingSupplierParty/i:Party/i:PartyIdentification/i:ID",
            namespaces=NS,
        )
        == "98765434"
    )
    assert (
        tree.findtext(
            "i:AccountingCustomerParty/i:Party/i:PartyTaxScheme/i:CompanyID",
            namespaces=NS,
        )
        == "CZ87654326"
    )


def test_deterministic_uuid_is_stable() -> None:
    inv = _load_invoice()
    u1 = deterministic_uuid(inv)
    u2 = deterministic_uuid(inv)
    assert u1 == u2
    assert len(u1) == 36


def test_invalid_xml_raises() -> None:
    try:
        validate(b"<not><valid></not>")
    except IsdocValidationError:
        return
    raise AssertionError("Should have raised")


def test_invalid_against_schema_raises() -> None:
    try:
        validate(b'<?xml version="1.0"?><Invoice xmlns="http://isdoc.cz/namespace/2013"/>')
    except IsdocValidationError:
        return
    raise AssertionError("Should have raised")


def test_schematron_rejects_curr_element_in_domestic_invoice() -> None:
    xml = render_isdoc(_load_invoice()).replace(
        b"<RefCurrRate>1</RefCurrRate>", b"<RefCurrRate>2</RefCurrRate>"
    )
    with pytest.raises(IsdocValidationError, match="RefCurrRate"):
        validate(xml)


def test_schematron_rejects_vat_mismatch_on_lines() -> None:
    xml = render_isdoc(_load_invoice()).replace(
        b"<VATApplicable>true</VATApplicable>\n    <ElectronicPossibility",
        b"<VATApplicable>false</VATApplicable>\n    <ElectronicPossibility",
    )
    with pytest.raises(IsdocValidationError, match="VATApplicable"):
        validate(xml)
