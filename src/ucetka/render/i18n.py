"""Popisky faktury v češtině a angličtině. ISDOC a evidence zůstávají česky — jazyk mění jen vzhled PDF."""
from __future__ import annotations

from collections.abc import Callable
from datetime import date
from decimal import Decimal

from ucetka.formatting import format_date, format_money
from ucetka.models import Invoice

LABELS: dict[str, dict[str, str]] = {
    "cs": {
        "tax_invoice": "Faktura — daňový doklad",
        "invoice": "Faktura",
        "tax_credit_note": "Opravný daňový doklad — dobropis",
        "credit_note": "Dobropis",
        "supplier": "Dodavatel",
        "customer": "Odběratel",
        "ico": "IČO",
        "dic": "DIČ",
        "vat_payer": "Plátce DPH",
        "corrects": "Opravuje doklad",
        "of_date": "ze dne",
        "reason": "Důvod opravy",
        "issued": "Vystaveno",
        "due": "Splatnost",
        "tax_point": "DUZP",
        "vs": "Variabilní symbol",
        "account": "Číslo účtu",
        "payment_form": "Forma úhrady",
        "currency": "Měna",
        "cnb_rate": "Kurz ČNB k DUZP",
        "qty": "Počet",
        "description": "Popis",
        "unit_price": "Jedn. cena",
        "vat_rate": "DPH",
        "base": "Základ",
        "vat": "DPH",
        "total": "Celkem",
        "rate": "Sazba",
        "subtotal": "Mezisoučet",
        "czk_recap": "Rekapitulace v Kč",
        "total_due": "Celkem k úhradě",
        "total_refund": "Celkem k vrácení",
        "issued_by": "Vystavil(a)",
        "qr": "QR Platba",
        "iban": "IBAN",
        "bic": "BIC/SWIFT",
    },
    "en": {
        "tax_invoice": "Invoice — tax document",
        "invoice": "Invoice",
        "tax_credit_note": "Credit note — corrective tax document",
        "credit_note": "Credit note",
        "supplier": "Supplier",
        "customer": "Customer",
        "ico": "Company ID",
        "dic": "VAT ID",
        "vat_payer": "VAT registered",
        "corrects": "Corrects document",
        "of_date": "of",
        "reason": "Reason",
        "issued": "Issue date",
        "due": "Due date",
        "tax_point": "Taxable supply",
        "vs": "Variable symbol",
        "account": "Bank account",
        "payment_form": "Payment method",
        "currency": "Currency",
        "cnb_rate": "CNB rate at tax point",
        "qty": "Quantity",
        "description": "Description",
        "unit_price": "Unit price",
        "vat_rate": "VAT rate",
        "base": "Total excl. VAT",
        "vat": "VAT",
        "total": "Total",
        "rate": "VAT rate",
        "subtotal": "Total",
        "czk_recap": "Summary in CZK",
        "total_due": "Total due",
        "total_refund": "Total to refund",
        "issued_by": "Issued by",
        "qr": "QR Platba",
        "iban": "IBAN",
        "bic": "BIC/SWIFT",
    },
}

_EN_PHRASES = {
    "Česká republika": "Czech Republic",
    "Převodem": "Bank transfer",
    "Fyzická osoba zapsaná v živnostenském rejstříku.":
        "Sole trader registered in the Trade Register of the Czech Republic.",
    "Nejsem plátce DPH.": "Not registered for VAT.",
    "Daň odvede zákazník. Reverse charge — čl. 196 směrnice 2006/112/ES.":
        "Reverse charge — VAT to be accounted for by the customer (Art. 196 of Directive 2006/112/EC).",
    "ks": "pcs",
    "hod": "h",
    "Kč": "CZK",
}


def labels(invoice: Invoice) -> dict[str, str]:
    return LABELS[invoice.language]


def phrase(invoice: Invoice, text: str | None) -> str | None:
    """Přeloží známé pevné české texty (ze supplier.yaml, modelu); volný text nechá být."""
    if invoice.language == "cs" or text is None:
        return text
    return _EN_PHRASES.get(text, text)


def money(invoice: Invoice) -> Callable[[Decimal], str]:
    """Anglická faktura má anglický formát čísel (170,080.00), stejně jako dřívější faktury z FakturaOnline."""
    if invoice.language == "cs":
        return format_money
    return lambda value: f"{Decimal(value).quantize(Decimal('0.01')):,.2f}"


def dates(invoice: Invoice) -> Callable[[date], str]:
    """Anglická faktura má anglické čísla, tak i datum bez tečkového českého formátu: 8 Oct 2026."""
    if invoice.language == "cs":
        return format_date
    return lambda value: f"{value.day} {value:%b} {value.year}"


def title(invoice: Invoice) -> str:
    t = labels(invoice)
    if invoice.is_credit_note:
        return t["tax_credit_note"] if invoice.is_tax_document else t["credit_note"]
    return t["tax_invoice"] if invoice.is_tax_document else t["invoice"]
