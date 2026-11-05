"""SID (QR Faktura) podle standardu KDP ČR: https://www.kdpcr.cz/informace/qr-faktura"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

from ucetka.formatting import account_to_iban
from ucetka.models import Invoice

# TB0 = základní sazba, TB1 = první snížená, TB2 = druhá snížená (od 2024 v ČR neexistuje).
_VAT_RATE_TO_SID_INDEX: dict[Decimal, str] = {
    Decimal(21): "0",
    Decimal(12): "1",
}

# Klíče, které už nese SPAYD — v X-INV se podle standardu neopakují.
_SHARED_WITH_SPAYD = ("AM", "VS", "DT", "CC", "ACC")


def _amount(value: Decimal) -> str:
    return f"{value:.2f}"


def _date_compact(d: date) -> str:
    return d.strftime("%Y%m%d")


def _vat_parts(invoice: Invoice) -> list[tuple[str, str]]:
    """Základy a daně jsou podle standardu vždy v CZK."""
    if not invoice.supplier.vat_payer or invoice.reverse_charge:
        return [("NTB", _amount(invoice.total_net_czk))]
    parts: list[tuple[str, str]] = []
    for row in sorted(invoice.vat_breakdown(), key=lambda r: r.rate, reverse=True):
        if row.rate == 0:
            parts.append(("NTB", _amount(row.base_czk)))
            continue
        idx = _VAT_RATE_TO_SID_INDEX.get(row.rate)
        if idx is None:
            raise ValueError(f"Sazba DPH {row.rate} % nemá v QR Faktuře klíč (podporováno 21, 12, 0).")
        parts.append((f"TB{idx}", _amount(row.base_czk)))
        parts.append((f"T{idx}", _amount(row.vat_czk)))
    return parts


def _sid_parts(invoice: Invoice) -> list[tuple[str, str]]:
    parts: list[tuple[str, str]] = [
        ("ID", invoice.number),
        ("DD", _date_compact(invoice.issue_date)),
        ("AM", _amount(invoice.total_gross)),
        ("VS", invoice.payment.variable_symbol),
    ]
    if invoice.supplier.dic:
        parts.append(("VII", invoice.supplier.dic))
    if invoice.supplier.ico:
        parts.append(("INI", invoice.supplier.ico))
    if invoice.customer.dic:
        parts.append(("VIR", invoice.customer.dic))
    if invoice.customer.ico:
        parts.append(("INR", invoice.customer.ico))
    parts.append(("DUZP", _date_compact(invoice.tax_point_date)))
    parts.append(("DT", _date_compact(invoice.due_date)))
    parts.extend(_vat_parts(invoice))
    parts.append(("CC", invoice.currency))
    if invoice.is_foreign_currency:
        parts.append(("FX", f"{invoice.exchange_rate:.3f}"))
        parts.append(("FXA", str(invoice.exchange_rate_amount)))
    acc = invoice.payment.bank_account
    parts.append(("ACC", account_to_iban(acc.prefix, acc.number, acc.bank_code)))
    return parts


def make_sind(invoice: Invoice) -> str:
    """Samostatný řetězec QR Faktury."""
    return "*".join(["SID", "1.0", *(f"{k}:{v}" for k, v in _sid_parts(invoice))])


def sind_for_spayd(invoice: Invoice) -> str:
    """Hodnota klíče X-INV pro QR Platbu+F: bez klíčů sdílených se SPAYD, `*` kódovaná jako %2A."""
    kept = [f"{k}:{v}" for k, v in _sid_parts(invoice) if k not in _SHARED_WITH_SPAYD]
    return "%2A".join(["SID", "1.0", *kept])
