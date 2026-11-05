from __future__ import annotations

from pathlib import Path

from lxml import etree

_SCHEMA_FILE = Path(__file__).resolve().parent / "schema" / "isdoc-invoice-6.0.2.xsd"
_schema_cache: etree.XMLSchema | None = None


class IsdocValidationError(Exception):
    pass


def _schema() -> etree.XMLSchema:
    global _schema_cache
    if _schema_cache is None:
        xsd_doc = etree.parse(str(_SCHEMA_FILE))
        _schema_cache = etree.XMLSchema(xsd_doc)
    return _schema_cache


_NS = {"i": "http://isdoc.cz/namespace/2013"}

# Prvky s částkou, které musí mít v cizoměnovém dokladu protějšek `<jméno>Curr` (isdoc-6.0.2.sch).
_CURR_PAIRED = (
    "LineExtensionAmount", "LineExtensionAmountTaxInclusive", "DepositAmount", "TaxableDepositAmount",
    "TaxInclusiveDepositAmount", "TaxAmount", "TaxableAmount", "TaxInclusiveAmount",
    "AlreadyClaimedTaxableAmount", "AlreadyClaimedTaxAmount", "AlreadyClaimedTaxInclusiveAmount",
    "DifferenceTaxableAmount", "DifferenceTaxAmount", "DifferenceTaxInclusiveAmount",
)


def _local(el: etree._Element) -> str:
    return etree.QName(el).localname


def schematron_errors(doc: etree._Element) -> list[str]:
    """Pravidla z přílohy C specifikace ISDOC 6.0.2 (isdoc-6.0.2.sch), přepsaná do Pythonu — lxml neumí XSLT2."""
    errors: list[str] = []
    foreign = doc.findtext("i:ForeignCurrencyCode", namespaces=_NS)
    if foreign is None:
        if doc.findtext("i:CurrRate", namespaces=_NS) != "1" or doc.findtext("i:RefCurrRate", namespaces=_NS) != "1":
            errors.append("Tuzemský doklad musí mít CurrRate i RefCurrRate rovno 1")
        if any(_local(el).endswith("Curr") for el in doc.iter() if isinstance(el.tag, str)):
            errors.append("Tuzemský doklad nesmí obsahovat prvky *Curr")
    else:
        if foreign == doc.findtext("i:LocalCurrencyCode", namespaces=_NS):
            errors.append("ForeignCurrencyCode se nesmí rovnat LocalCurrencyCode")
        for el in doc.iter():
            if isinstance(el.tag, str) and _local(el) in _CURR_PAIRED:
                parent = el.getparent()
                if parent.find(f"i:{_local(el)}Curr", namespaces=_NS) is None:
                    errors.append(f"Cizoměnový doklad: {_local(parent)}/{_local(el)} nemá {_local(el)}Curr")
    if doc.findtext("i:VATApplicable", namespaces=_NS) == "false":
        line_flags = doc.xpath("i:InvoiceLines/i:InvoiceLine/i:ClassifiedTaxCategory/i:VATApplicable/text()", namespaces=_NS)
        if any(flag != "false" for flag in line_flags):
            errors.append("Doklad s VATApplicable=false musí mít VATApplicable=false i na řádcích")
    if doc.findtext("i:DocumentType", namespaces=_NS) in ("2", "3", "6") and doc.find(
        "i:OriginalDocumentReferences/*", namespaces=_NS
    ) is None:
        errors.append("Typ dokladu 2, 3 a 6 musí mít OriginalDocumentReferences")
    return errors


def validate(xml: bytes) -> None:
    try:
        doc = etree.fromstring(xml)
    except etree.XMLSyntaxError as exc:
        raise IsdocValidationError(f"Nevalidní XML: {exc}") from exc
    schema = _schema()
    if not schema.validate(doc):
        errors = "\n".join(f"  line {e.line}: {e.message}" for e in schema.error_log)
        raise IsdocValidationError(f"ISDOC neprošel XSD validací:\n{errors}")
    rule_errors = schematron_errors(doc)
    if rule_errors:
        raise IsdocValidationError("ISDOC porušuje pravidla isdoc-6.0.2.sch:\n" + "\n".join(f"  {e}" for e in rule_errors))
