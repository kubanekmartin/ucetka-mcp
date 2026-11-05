from __future__ import annotations

import uuid as uuid_mod
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from ucetka.banks import bank_bic as _bank_bic
from ucetka.banks import bank_name as _bank_name
from ucetka.formatting import account_to_iban
from ucetka.models import Invoice

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"

_ISDOC_NS = uuid_mod.UUID("6BA7B810-9DAD-11D1-80B4-00C04FD430C8")  # uuid.NAMESPACE_DNS


def deterministic_uuid(invoice: Invoice) -> str:
    seed = f"isdoc:{invoice.supplier.ico or ''}:{invoice.number}:{invoice.issue_date.isoformat()}"
    return str(uuid_mod.uuid5(_ISDOC_NS, seed)).upper()


def render_isdoc(invoice: Invoice) -> bytes:
    env = Environment(
        loader=FileSystemLoader(TEMPLATES_DIR),
        autoescape=select_autoescape(["xml"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )
    template = env.get_template("invoice.isdoc.j2")
    iban = account_to_iban(
        invoice.payment.bank_account.prefix,
        invoice.payment.bank_account.number,
        invoice.payment.bank_account.bank_code,
    )
    xml = template.render(
        inv=invoice,
        uuid=deterministic_uuid(invoice),
        iban=iban,
        bank_name=_bank_name(invoice.payment.bank_account.bank_code),
        bic=_bank_bic(invoice.payment.bank_account.bank_code),
        gross_czk=invoice.total_net_czk + invoice.total_vat_czk,
        unit_czk=lambda price: price * (invoice.exchange_rate or 1) / invoice.exchange_rate_amount,
    )
    return xml.encode("utf-8")
