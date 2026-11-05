"""Export faktury do Pohoda XML (StormWare schema version_2).

Spec: https://www.stormware.cz/pohoda/xml/
Pozn.: Pohoda historicky vyžaduje encoding Windows-1250 (ne UTF-8).
Výstup se vždy validuje proti vendorovanému invoice.xsd (export/schema/).
"""
from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from ucetka.export.validator import validate
from ucetka.models import Invoice

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"

_VAT_RATE_TO_POHODA: dict[Decimal, str] = {
    Decimal(0): "none",
    Decimal(12): "low",
    Decimal(21): "high",
}


_ITEM_TEXT_MAX = 90


def _rate_to_pohoda(rate: Decimal) -> str:
    if rate not in _VAT_RATE_TO_POHODA:
        raise ValueError(f"Sazba DPH {rate} % nemá v Pohodě ekvivalent (podporováno 21, 12, 0).")
    return _VAT_RATE_TO_POHODA[rate]


def _check_item_texts(invoice: Invoice) -> None:
    for line in invoice.lines:
        if len(line.description) > _ITEM_TEXT_MAX:
            raise ValueError(
                f"Popis položky má {len(line.description)} znaků, Pohoda bere max. {_ITEM_TEXT_MAX}: "
                f"{line.description!r}"
            )


def render_pohoda_xml(
    invoice: Invoice,
    *,
    application: str = "ucetka",
    eu_classification: str | None = None,
) -> bytes:
    """`eu_classification` = zkratka členění DPH z Pohody pro službu do EU (Nastavení → Členění DPH).

    Kód se liší podle verze/agendy Pohody, proto ho nehádáme a u reverse charge je povinný.
    """
    if invoice.is_credit_note:
        raise ValueError("Dobropis export do Pohody zatím neumí")
    if invoice.reverse_charge and not eu_classification:
        raise ValueError(
            "Reverse charge faktura potřebuje zkratku členění DPH z Pohody pro službu do EU "
            "(Pohoda → Nastavení → Členění DPH, ř. 21 přiznání)."
        )
    env = Environment(
        loader=FileSystemLoader(TEMPLATES_DIR),
        autoescape=select_autoescape(["xml"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )
    template = env.get_template("invoice.pohoda.xml.j2")

    rows_by_rate: dict[Decimal, dict[str, Decimal]] = {}
    for row in invoice.vat_breakdown():
        rows_by_rate[row.rate] = {"base": row.base_czk, "vat": row.vat_czk, "total": row.base_czk + row.vat_czk}

    summary = {
        "none_base": rows_by_rate.get(Decimal(0), {}).get("base", Decimal(0)),
        "low_base": rows_by_rate.get(Decimal(12), {}).get("base", Decimal(0)),
        "low_vat": rows_by_rate.get(Decimal(12), {}).get("vat", Decimal(0)),
        "low_total": rows_by_rate.get(Decimal(12), {}).get("total", Decimal(0)),
        "high_base": rows_by_rate.get(Decimal(21), {}).get("base", Decimal(0)),
        "high_vat": rows_by_rate.get(Decimal(21), {}).get("vat", Decimal(0)),
        "high_total": rows_by_rate.get(Decimal(21), {}).get("total", Decimal(0)),
        "rate_to_pohoda": _rate_to_pohoda,
    }

    _check_item_texts(invoice)
    xml = template.render(inv=invoice, application=application, eu_classification=eu_classification, **summary)
    try:
        data = xml.encode("windows-1250")
    except UnicodeEncodeError as exc:
        bad = exc.object[exc.start : exc.end]
        raise ValueError(f"Znak {bad!r} nejde zapsat ve windows-1250, které Pohoda vyžaduje.") from exc
    validate(data)
    return data
