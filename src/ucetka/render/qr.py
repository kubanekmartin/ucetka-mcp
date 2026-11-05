from __future__ import annotations

import base64
from decimal import Decimal
from io import BytesIO

import qrcode
from qrplatba import SpaydGenerator

from ucetka.formatting import account_to_iban
from ucetka.models import BankAccount, Invoice
from ucetka.render.sind import sind_for_spayd


def _build_kwargs(invoice: Invoice) -> dict:
    acc = invoice.payment.bank_account
    iban = account_to_iban(acc.prefix, acc.number, acc.bank_code)
    return {
        "account": iban,
        "amount": invoice.total_gross,
        "currency": invoice.currency,
        "x_vs": invoice.payment.variable_symbol,
        "message": f"Faktura {invoice.number}",
        "due_date": invoice.due_date,
    }


def make_spayd(invoice: Invoice, *, with_invoice: bool = False) -> str:
    """Sestaví SPAYD. S `with_invoice=True` přidá X-INV se SID (QR Faktura)."""
    base = SpaydGenerator(**_build_kwargs(invoice)).get_text()
    if with_invoice:
        base = f"{base}*X-INV:{sind_for_spayd(invoice)}"
    return base


def make_qr_png(invoice: Invoice, *, box_size: int = 8, with_invoice: bool = False) -> bytes:
    """Vyrenderuje SPAYD jako PNG QR kód přes čistou `qrcode` knihovnu.

    Pozn.: `qrplatba.make_image()` má bug — jím vyrenderovaný PNG se v opencv
    QR detektoru nedekóduje (mobilní bankingy s ním mívají problém). Proto
    používáme přímo `qrcode` lib, která vrací standardní validní QR.
    """
    return _png(make_spayd(invoice, with_invoice=with_invoice), box_size)


def _png(spayd: str, box_size: int) -> bytes:
    qr = qrcode.QRCode(
        border=2,
        box_size=box_size,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
    )
    qr.add_data(spayd)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    buf = BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def make_qr_data_uri(invoice: Invoice, *, box_size: int = 8, with_invoice: bool = False) -> str:
    png = make_qr_png(invoice, box_size=box_size, with_invoice=with_invoice)
    b64 = base64.b64encode(png).decode("ascii")
    return f"data:image/png;base64,{b64}"


def make_payment_spayd(account: BankAccount, amount: Decimal, variable_symbol: str, message: str) -> str:
    """QR platba bez faktury — zálohy ČSSZ / FÚ podle údajů z jejich dopisu."""
    iban = account_to_iban(account.prefix, account.number, account.bank_code)
    return SpaydGenerator(
        account=iban, amount=amount, currency="CZK", x_vs=variable_symbol, message=message
    ).get_text()


def make_payment_qr_data_uri(account: BankAccount, amount: Decimal, variable_symbol: str, message: str) -> str:
    png = _png(make_payment_spayd(account, amount, variable_symbol, message), 8)
    return f"data:image/png;base64,{base64.b64encode(png).decode('ascii')}"
