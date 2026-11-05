from __future__ import annotations

from pathlib import Path

from lxml import etree

_SCHEMA_FILE = Path(__file__).resolve().parent / "schema" / "invoice.xsd"
_INV_NS = "http://www.stormware.cz/schema/version_2/invoice.xsd"
_schema_cache: etree.XMLSchema | None = None


class PohodaValidationError(Exception):
    pass


def _schema() -> etree.XMLSchema:
    global _schema_cache
    if _schema_cache is None:
        _schema_cache = etree.XMLSchema(etree.parse(str(_SCHEMA_FILE)))
    return _schema_cache


def validate(xml: bytes) -> None:
    """Validuje každý `inv:invoice` v dataPacku proti oficiálnímu Stormware invoice.xsd.

    Obal `dat:dataPack` se nevaliduje — data.xsd importuje ~70 dalších schémat.
    """
    doc = etree.fromstring(xml)
    invoices = doc.findall(f".//{{{_INV_NS}}}invoice")
    if not invoices:
        raise PohodaValidationError("Pohoda XML neobsahuje žádný inv:invoice")
    schema = _schema()
    for inv in invoices:
        if not schema.validate(etree.ElementTree(inv)):
            errors = "\n".join(f"  line {e.line}: {e.message}" for e in schema.error_log)
            raise PohodaValidationError(f"Pohoda XML neprošlo XSD validací:\n{errors}")
