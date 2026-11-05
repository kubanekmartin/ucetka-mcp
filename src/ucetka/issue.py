"""Vystavení faktury a dobropisu: QR + ISDOC + PDF/A-3a → soubor → zápis do evidence."""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

from ucetka import ledger
from ucetka.config import load_supplier, output_dir
from ucetka.isdoc.embed import embed_isdoc
from ucetka.isdoc.generator import render_isdoc
from ucetka.isdoc.validator import validate
from ucetka.models import Invoice, LineItem, OriginalDocument
from ucetka.render.pdf import render_pdf
from ucetka.render.qr import make_qr_data_uri


def render_invoice_pdf(
    inv: Invoice,
    *,
    qr: bool = True,
    qr_plus_f: bool = True,
    isdoc: bool = True,
    pdf_a3: bool = True,
) -> bytes:
    if inv.payment.bank_account is None:
        raise ValueError("Faktura nemá bankovní účet — doplň bank_account do supplier.yaml nebo do faktury.")
    qr = qr and not inv.is_credit_note
    qr_uri = make_qr_data_uri(inv, with_invoice=qr_plus_f) if qr else None
    qr_label = ("QR Platba + F" if qr_plus_f else "QR Platba") if qr else None
    isdoc_xml = render_isdoc(inv) if isdoc else None
    if isdoc_xml is not None:
        validate(isdoc_xml)
    pdf = render_pdf(inv, qr_data_uri=qr_uri, qr_label=qr_label, has_isdoc=isdoc, pdf_a3=pdf_a3)
    return embed_isdoc(pdf, isdoc_xml) if isdoc_xml is not None else pdf


def issue(inv: Invoice, *, out_dir: Path | None = None, **render_opts: bool) -> ledger.InvoiceRecord:
    if ledger.exists(inv.number):
        raise ValueError(f"Faktura {inv.number} už v evidenci je — číslo nelze použít znovu")
    folder = out_dir or output_dir() / inv.tax_point_date.strftime("%y_%m_vydane")
    target = folder / f"{'dobropis' if inv.is_credit_note else 'faktura'}_{inv.number}.pdf"
    if target.exists():
        raise ValueError(f"{target} už existuje — nepřepisuji vydanou fakturu")
    pdf = render_invoice_pdf(inv, **render_opts)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(pdf)
    return ledger.save(inv, target)


def build_credit_note(
    original_number: str,
    reason: str,
    *,
    lines: list[LineItem] | None = None,
    issue_date: date | None = None,
    due_days: int = 14,
) -> Invoice:
    """Dobropis k faktuře z evidence: celé položky původní faktury, nebo jen `lines` (kladné částky snížení)."""
    original = ledger.get(original_number)
    orig = original.invoice
    if orig.is_credit_note:
        raise ValueError(f"{original_number} je sám dobropis — opravuje se původní faktura")
    if original.status == "cancelled":
        raise ValueError(f"Faktura {original_number} je stornovaná")
    issued = issue_date or date.today()
    if issued < orig.issue_date:
        raise ValueError("Dobropis nemůže být vystaven před původní fakturou")
    credited = sum(
        (r.invoice.total_net for r in ledger.list_records()
         if r.status != "cancelled" and r.invoice.original and r.invoice.original.number == original_number),
        Decimal(0),
    )
    number = ledger.next_number(issued)
    supplier = load_supplier()
    note = Invoice(
        document_type="credit_note",
        original=OriginalDocument(number=orig.number, issue_date=orig.issue_date),
        correction_reason=reason,
        number=number,
        issue_date=issued,
        tax_point_date=issued,
        due_date=issued + timedelta(days=due_days),
        supplier=orig.supplier,
        customer=orig.customer,
        payment={"bank_account": supplier.bank_account or orig.payment.bank_account, "variable_symbol": number},
        lines=lines or orig.lines,
        currency=orig.currency,
        exchange_rate=orig.exchange_rate,
        exchange_rate_amount=orig.exchange_rate_amount,
        reverse_charge=orig.reverse_charge,
        issued_by=orig.issued_by,
    )
    if credited + note.total_net > orig.total_net:
        raise ValueError(
            f"Dobropisy by přesáhly základ faktury {original_number} "
            f"({credited + note.total_net} > {orig.total_net})"
        )
    return note
