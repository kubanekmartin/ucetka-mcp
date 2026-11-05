from __future__ import annotations

from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from ucetka.banks import bank_bic
from ucetka.formatting import account_to_iban, format_iban, format_money_with_currency, format_percent
from ucetka.models import Invoice
from ucetka.render import i18n

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"


def _env() -> Environment:
    return Environment(
        loader=FileSystemLoader(TEMPLATES_DIR),
        autoescape=select_autoescape(["html", "xml"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )


def _iban(invoice: Invoice) -> str:
    acc = invoice.payment.bank_account
    return format_iban(account_to_iban(acc.prefix, acc.number, acc.bank_code))


def render_html(
    invoice: Invoice,
    *,
    qr_data_uri: str | None = None,
    qr_label: str | None = None,
    has_isdoc: bool = False,
) -> str:
    env = _env()
    template = env.get_template("invoice.html.j2")
    css = (TEMPLATES_DIR / "invoice.css").read_text(encoding="utf-8")
    return template.render(
        inv=invoice,
        css=css,
        qr_data_uri=qr_data_uri,
        qr_label=qr_label,
        has_isdoc=has_isdoc,
        t=i18n.labels(invoice),
        tr=lambda text: i18n.phrase(invoice, text),
        title=i18n.title(invoice),
        fmt_money=i18n.money(invoice),
        fmt_money_cur=format_money_with_currency,
        fmt_pct=format_percent,
        fmt_date=i18n.dates(invoice),
        iban=_iban(invoice),
        bic=bank_bic(invoice.payment.bank_account.bank_code),
        fmt_rate=lambda r: f"{r:.3f}".replace(".", ","),
    )


def render_pdf(
    invoice: Invoice,
    *,
    qr_data_uri: str | None = None,
    qr_label: str | None = None,
    has_isdoc: bool = False,
    pdf_a3: bool = True,
) -> bytes:
    from weasyprint import HTML  # lazy: dlopen pango/cairo

    html = render_html(
        invoice,
        qr_data_uri=qr_data_uri,
        qr_label=qr_label,
        has_isdoc=has_isdoc,
    )
    kwargs: dict = {}
    if pdf_a3:
        kwargs["pdf_variant"] = "pdf/a-3a"
    return HTML(string=html, base_url=str(TEMPLATES_DIR)).write_pdf(**kwargs)
