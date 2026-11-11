"""MCP server for ucetka — invoicing and bookkeeping from an AI chat. install.sh registers it in Claude Desktop (see README)."""

from __future__ import annotations

import subprocess
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Literal

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field

from ucetka import __version__, ares, importer, ledger, mail, overview
from ucetka import expenses as expenses_mod
from ucetka.cnb import fetch_rate
from ucetka.config import customers_file, load_supplier
from ucetka.customers import add_customer as add_customer_to_book
from ucetka.customers import get_customer, load_customers
from ucetka.export import dph, kniha
from ucetka.export.pohoda import render_pohoda_xml
from ucetka.formatting import (
    account_to_iban,
    format_date,
    format_iban,
    format_money,
)
from ucetka.issue import build_credit_note, issue
from ucetka.models import Address, BankAccount, Invoice, LineItem, Party
from ucetka.payments import KIND_LABELS, Kind, Payment
from ucetka.payments import record_payment as store_payment
from ucetka.render.qr import make_spayd

INSTRUCTIONS = """\
ucetka: invoicing and bookkeeping for a Czech sole trader (OSVČ), working on local files only.
Data lives in ~/.ucetka (supplier.yaml, customers.yaml, tax.yaml, invoices/, expenses/, payments.json) and in
~/Documents/Účetnictví/{Vydané faktury, Přijaté faktury, Přehledy} (issued PDFs, received PDFs, reports).
Precondition: the supplier profile must exist — run `ucetka init` in a terminal first; most tools fail without it.

Typical workflows:
- Invoicing: list_customers / lookup_company / add_customer → peek_invoice_number → issue_invoice →
  draft_invoice_email → (after the bank statement) mark_paid. Wrong invoice not yet sent: cancel_invoice;
  already delivered: issue_credit_note.
- Received and foreign documents: sync_invoices → list_import_issues → read each PDF →
  add_expense (received) / add_manual_invoice (issued). Paid expense: mark_expense_paid.
- Taxes: tax_overview for the current state, advance_schedule for ČSSZ (social insurance) advances,
  record_payment after paying a tax/insurance advance, export_vat_return each VAT quarter, export_book for
  the yearly income/expense book, open_dashboard for charts.

Guarantees: nothing is ever filed with authorities (VAT files are only written to disk for a manual upload)
and no e-mail is ever sent (only a draft is opened). Network access: ARES company registry (lookup_company,
add_customer, issue_invoice with customer_ico) and ČNB exchange rates (issue_invoice in a foreign currency).
macOS-only: draft_invoice_email (Mail.app) and open_dashboard (browser).
Confirm with the user before destructive tools (cancel_invoice, remove_expense) and before issuing documents
(issue_invoice, issue_credit_note); verify amounts and the customer first. Ask instead of guessing tax details
such as reverse charge or private use.
"""

mcp = FastMCP("ucetka", instructions=INSTRUCTIONS)
# FastMCP has no public version option; without this, initialize reports the mcp SDK's version.
mcp._mcp_server.version = __version__


def _hints(*, read_only: bool, destructive: bool, idempotent: bool, open_world: bool) -> ToolAnnotations:
    return ToolAnnotations(
        readOnlyHint=read_only,
        destructiveHint=destructive,
        idempotentHint=idempotent,
        openWorldHint=open_world,
    )


_READ_LOCAL = _hints(read_only=True, destructive=False, idempotent=True, open_world=False)

_ISO_DATE = "YYYY-MM-DD"
_INVOICE_NO_RE = r"^[A-Za-z0-9_-]{1,20}$"
_EXPENSE_ID_RE = r"^[A-Za-z0-9_-]{1,40}$"
_ARES_ICO_RE = r"^\d{8}$"


class LineInput(BaseModel):
    description: str = Field(
        description="Item text printed on the document, e.g. 'Přednáška o AI' or 'AI workshop'."
    )
    unit_price: Decimal = Field(
        gt=0,
        description="Price per unit WITHOUT VAT, in the document currency (e.g. 1500 = 1 500 CZK or 1 500 EUR). "
        "Must be positive, also on credit notes.",
    )
    quantity: Decimal = Field(
        Decimal(1), gt=0, description="Number of units, e.g. 10 hours. Default 1."
    )
    unit: str = Field(
        "ks", description="Unit label printed on the document: 'ks' (pieces, default), 'h' (hours), 'měsíc' (month)…"
    )
    vat_rate: Decimal | None = Field(
        None,
        description="VAT rate in percent: 21, 12 or 0. Omitted = 21 if the supplier is a VAT payer, 0 for a "
        "non-payer or reverse charge (on a credit note: the rate of the original invoice's first line). "
        "A non-payer or a reverse-charge invoice must use 0.",
    )


def _money(value: Decimal, inv: Invoice) -> str:
    return f"{format_money(value)} {inv.currency_symbol}"


def _party_summary(p: Party) -> str:
    ids = (
        ", ".join(x for x in (p.ico and f"IČO {p.ico}", p.dic and f"DIČ {p.dic}") if x)
        or "bez IČO"
    )
    vat = "plátce DPH" if p.vat_payer else "neplátce DPH"
    return f"{p.name} ({ids}, {vat}) — {p.address.street}, {p.address.zip} {p.address.city}"


def _record_summary(r: ledger.InvoiceRecord) -> str:
    inv = r.invoice
    state = (
        "PO SPLATNOSTI"
        if r.overdue
        else {
            "issued": "nezaplacena",
            "paid": f"zaplacena {r.paid_date}",
            "cancelled": f"STORNO ({r.cancel_reason})",
        }[r.status]
    )
    return (
        f"{inv.number} · {format_date(inv.issue_date)} · {inv.customer.name} · "
        f"{_money(inv.total_gross, inv)} · splatnost {format_date(inv.due_date)} · {state}"
    )


@mcp.tool(title="Show supplier profile", annotations=_READ_LOCAL)
def get_supplier() -> str:
    """Return the user's own business details (the invoicing supplier): name, IČO (company ID), DIČ (VAT ID),
    VAT-payer status, address and the default bank account used on new invoices.

    Read-only; reads ~/.ucetka/supplier.yaml, no network. Use it to check who invoices are issued as or which
    account issue_invoice will use by default; for customers use list_customers, for any other company
    lookup_company. Fails with FileNotFoundError if the profile has not been created with `ucetka init`.
    """
    s = load_supplier()
    lines = [f"Dodavatel: {_party_summary(s.party())}"]
    if s.bank_account:
        lines.append(f"Výchozí účet: {s.bank_account.as_string()}")
    return "\n".join(lines)


@mcp.tool(title="List saved customers", annotations=_READ_LOCAL)
def list_customers() -> str:
    """List the customer address book (~/.ucetka/customers.yaml): one line per customer with its customer_id,
    name, IČO (company ID), DIČ (VAT ID), VAT-payer status, address and e-mail.

    Read-only, no network. Call it before issue_invoice to find the customer_id to pass. If the customer is
    missing, use lookup_company to check them in the ARES registry and add_customer to save them. Returns a
    hint instead of a list when the book is empty.
    """
    book = load_customers()
    if not book:
        return f"Adresář je prázdný ({customers_file()}). Přidej odběratele přes add_customer."
    return "\n".join(
        f"• {cid}: {_party_summary(p)}{f' · {p.email}' if p.email else ''}"
        for cid, p in book.items()
    )


@mcp.tool(
    title="Look up company in ARES",
    annotations=_hints(read_only=True, destructive=False, idempotent=True, open_world=True),
)
def lookup_company(
    ico: Annotated[
        str,
        Field(
            pattern=_ARES_ICO_RE,
            description="Czech IČO (company ID): exactly 8 digits incl. leading zeros, e.g. '27082440'.",
        ),
    ],
) -> str:
    """Look up a Czech company in the public ARES registry (Ministry of Finance) by IČO (company ID) and return
    its official name, registered address, DIČ (VAT ID) and whether it is an active VAT payer.

    Read-only: makes one HTTPS request to ares.gov.cz and saves nothing. Use it to verify a customer before
    invoicing; to also store the company in the address book use add_customer instead (it does the same lookup).
    Fails with CompanyNotFound if the IČO does not exist, or with an HTTP/network error if ARES is unreachable.
    """
    return _party_summary(ares.lookup(ico))


@mcp.tool(
    title="Add customer from ARES",
    annotations=_hints(read_only=False, destructive=False, idempotent=False, open_world=True),
)
def add_customer(
    customer_id: Annotated[
        str,
        Field(
            description="New short key for the address book, later passed to issue_invoice as customer_id, "
            "e.g. 'vzorova-firma'. Must not exist yet."
        ),
    ],
    ico: Annotated[
        str,
        Field(
            pattern=_ARES_ICO_RE,
            description="Customer's IČO (company ID), exactly 8 digits, e.g. '27082440'. Name, address, DIČ "
            "and VAT status are filled in from ARES.",
        ),
    ],
    email: Annotated[
        str | None,
        Field(
            description="Address that invoices are e-mailed to; draft_invoice_email uses it as the default "
            "recipient. Omitted = no e-mail stored."
        ),
    ] = None,
) -> str:
    """Save a Czech company to the customer address book (~/.ucetka/customers.yaml) under customer_id, with
    name, address, DIČ (VAT ID) and VAT status fetched from the ARES registry by IČO (company ID).

    Side effects: one HTTPS request to ares.gov.cz, then appends to customers.yaml. Not idempotent: if
    customer_id already exists it raises an error and changes nothing (existing entries are never
    overwritten or updated). Fails if the IČO is unknown in ARES or ARES is unreachable. Use lookup_company to
    only check a company, list_customers to see saved ones. Foreign customers without an IČO cannot be
    added here; pass their details directly to issue_invoice. Returns a one-line summary of the saved customer.
    """
    party = add_customer_to_book(customer_id, ares.lookup(ico).model_copy(update={"email": email}))
    return f"Přidáno {customer_id}: {_party_summary(party)}"


@mcp.tool(title="Preview next invoice number", annotations=_READ_LOCAL)
def peek_invoice_number(
    issue_date: Annotated[
        str | None,
        Field(
            description=f"Issue date {_ISO_DATE}, e.g. '2026-10-04'; the number series restarts every month. "
            "Omitted = today."
        ),
    ] = None,
) -> str:
    """Return the invoice number that issue_invoice would assign next, in the form YYMMNNN (year, month of the
    issue date, 3-digit sequence), e.g. '2610001'.

    Read-only and reserves nothing: the number is computed again when issue_invoice runs, so it can change if
    another invoice is issued in between. Takes into account the ledger (~/.ucetka/invoices) and PDFs already in
    the issued-invoices folder. Use it to tell the user the number in advance; to create the invoice use
    issue_invoice.
    """
    return ledger.next_number(
        date.fromisoformat(issue_date) if issue_date else date.today()
    )


@mcp.tool(
    title="Issue invoice (PDF + ISDOC)",
    annotations=_hints(read_only=False, destructive=False, idempotent=False, open_world=True),
)
def issue_invoice(
    lines: Annotated[
        list[LineInput],
        Field(min_length=1, description="Invoice items (at least one); prices are per unit without VAT."),
    ],
    customer_id: Annotated[
        str | None,
        Field(
            description="Customer key from the address book (see list_customers). Takes precedence over "
            "customer_ico and the customer_name/street/city/zip fields."
        ),
    ] = None,
    customer_ico: Annotated[
        str | None,
        Field(
            pattern=_ARES_ICO_RE,
            description="IČO (company ID, 8 digits) of a Czech customer not in the address book; details are "
            "fetched from ARES (network). Used only when customer_id is omitted.",
        ),
    ] = None,
    customer_name: Annotated[
        str | None,
        Field(
            description="Name of a customer without IČO (e.g. a foreign company). Used only when customer_id and "
            "customer_ico are omitted, and then customer_street, customer_city and customer_zip are required too."
        ),
    ] = None,
    customer_dic: Annotated[
        str | None,
        Field(
            description="DIČ / EU VAT ID of a customer entered by name, e.g. 'DE123456789'. If given, the customer "
            "is treated as a VAT payer; required for reverse_charge (must not start with CZ)."
        ),
    ] = None,
    customer_street: Annotated[
        str | None,
        Field(description="Street and number of a customer entered by name, e.g. 'Musterstraße 1'."),
    ] = None,
    customer_city: Annotated[
        str | None, Field(description="City of a customer entered by name, e.g. 'Berlin'.")
    ] = None,
    customer_zip: Annotated[
        str | None, Field(description="Postal code of a customer entered by name, e.g. '10115'.")
    ] = None,
    customer_country: Annotated[
        str | None,
        Field(
            description="Country name printed on the invoice for a customer entered by name, e.g. 'Německo'. "
            "Omitted = 'Česká republika'."
        ),
    ] = None,
    customer_country_code: Annotated[
        str,
        Field(
            description="ISO 3166 alpha-2 country code of a customer entered by name, e.g. 'DE'. Default 'CZ'; "
            "must not be CZ for reverse_charge."
        ),
    ] = "CZ",
    issue_date: Annotated[
        str | None,
        Field(description=f"Issue date {_ISO_DATE}. Omitted = today. Also determines the invoice number month."),
    ] = None,
    tax_point_date: Annotated[
        str | None,
        Field(
            description=f"DUZP (tax point date) {_ISO_DATE}. Omitted = issue_date. Decides the VAT period, the ČNB "
            "rate date and the output folder."
        ),
    ] = None,
    due_days: Annotated[
        int, Field(ge=0, description="Days from issue_date to the due date. Default 14; 0 = due on issue.")
    ] = 14,
    currency: Annotated[
        str,
        Field(
            pattern=r"^[A-Z]{3}$",
            description="ISO 4217 currency code, e.g. 'CZK' (default) or 'EUR'. For anything other than CZK the "
            "ČNB exchange rate valid on the DUZP is downloaded from api.cnb.cz.",
        ),
    ] = "CZK",
    reverse_charge: Annotated[
        bool,
        Field(
            description="True for a service to a VAT payer in another EU country: no Czech VAT, the customer "
            "self-assesses. Requires customer_dic (non-CZ), a non-CZ country and vat_rate 0 on all lines. "
            "Default False."
        ),
    ] = False,
    note: Annotated[
        str | None, Field(description="Free text printed on the invoice. Omitted = none.")
    ] = None,
    bank_account: Annotated[
        str | None,
        Field(
            description="Czech bank account to print instead of the supplier's default, format "
            "'[prefix-]number/bankcode', e.g. '19-2000145399/0100'. Required if supplier.yaml has no default."
        ),
    ] = None,
    invoice_number: Annotated[
        str | int | None,
        Field(
            description="Force a specific number (letters, digits, '-' or '_', max 20 chars), e.g. '2610005'. "
            "Omitted = next free number YYMMNNN (see peek_invoice_number). Fails if it is already used."
        ),
    ] = None,
    language: Annotated[
        Literal["cs", "en"],
        Field(description="Language of the PDF: 'cs' Czech (default) or 'en' English; ledger and ISDOC stay Czech."),
    ] = "cs",
) -> str:
    """Issue a new invoice: render a PDF/A-3a with an embedded ISDOC 6.0.2 e-invoice and a QR Platba+F payment
    code, save it and record it in the ledger. Returns a summary: PDF path, number, items, totals, due date and
    variable symbol.

    Side effects: writes ~/Documents/Účetnictví/Vydané faktury/<YY_MM_vydane>/faktura_<number>.pdf (folder by
    DUZP, tax point date) and ~/.ucetka/invoices/<number>.json; calls ARES when customer_ico is used and the ČNB
    rate API when currency is not CZK. Not idempotent: every call creates a new invoice with a new number, so
    never retry blindly — check list_invoices first. Validation errors (e.g. reverse-charge rules, missing
    customer or bank account) raise before anything is written and consume no number; an existing number or PDF
    is never overwritten. Needs supplier config from `ucetka init`.
    Confirm the customer (list_customers / lookup_company) and amounts with the user first. Use
    add_manual_invoice instead to record an invoice whose PDF already exists, and issue_credit_note to correct a
    delivered invoice. Next step is usually draft_invoice_email.
    """
    supplier_cfg = load_supplier()
    supplier = supplier_cfg.party()

    if customer_id:
        customer = get_customer(customer_id)
    elif customer_ico:
        customer = ares.lookup(customer_ico)
    elif customer_name and customer_street and customer_city and customer_zip:
        customer = Party(
            name=customer_name,
            dic=customer_dic,
            vat_payer=bool(customer_dic),
            address=Address(
                street=customer_street,
                city=customer_city,
                zip=customer_zip,
                country=customer_country or "Česká republika",
                country_code=customer_country_code,
            ),
        )
    else:
        raise ValueError(
            "Zadej customer_id, customer_ico, nebo customer_name + ulici, město a PSČ."
        )

    if bank_account:
        account = BankAccount.parse(bank_account)
    elif supplier_cfg.bank_account is not None:
        account = supplier_cfg.bank_account
    else:
        raise ValueError(
            "supplier.yaml nemá výchozí bank_account — zadej bank_account."
        )

    issued = date.fromisoformat(issue_date) if issue_date else date.today()
    duzp = date.fromisoformat(tax_point_date) if tax_point_date else issued
    default_rate = (
        Decimal(21) if supplier.vat_payer and not reverse_charge else Decimal(0)
    )
    rate, rate_amount = fetch_rate(currency, duzp) if currency != "CZK" else (None, 1)
    number = str(invoice_number) if invoice_number else ledger.next_number(issued)

    inv = Invoice(
        number=number,
        issue_date=issued,
        tax_point_date=duzp,
        due_date=issued + timedelta(days=due_days),
        supplier=supplier,
        customer=customer,
        payment={"bank_account": account, "variable_symbol": number},
        lines=[
            {
                "quantity": ln.quantity,
                "unit": ln.unit,
                "description": ln.description,
                "unit_price_net": ln.unit_price,
                "vat_rate": default_rate if ln.vat_rate is None else ln.vat_rate,
            }
            for ln in lines
        ],
        currency=currency,
        language=language,
        exchange_rate=rate,
        exchange_rate_amount=rate_amount,
        reverse_charge=reverse_charge,
        note=note,
        issued_by=supplier.name,
    )
    record = issue(inv)

    out = [
        f"Vystaveno: {record.pdf_path}",
        f"Číslo {inv.number} · odběratel {customer.name}",
    ]
    for ln in inv.lines:
        out.append(
            f"  {ln.quantity} {ln.unit} × {_money(ln.unit_price_net, inv)} — {ln.description} ({ln.vat_rate} %)"
        )
    out.append(
        f"Základ {_money(inv.total_net, inv)} · DPH {_money(inv.total_vat, inv)} · "
        f"celkem {_money(inv.total_gross, inv)}"
    )
    if inv.is_foreign_currency:
        out.append(
            f"Kurz ČNB k {format_date(duzp)}: {inv.exchange_rate_amount} {currency} = {inv.exchange_rate} Kč"
        )
    out.append(
        f"Splatnost {format_date(inv.due_date)} · VS {inv.payment.variable_symbol}"
    )
    if not customer.email:
        out.append(
            "Odběratel nemá e-mail — pro draft_invoice_email ho zadej parametrem `to`."
        )
    return "\n".join(out)


@mcp.tool(title="List issued invoices", annotations=_READ_LOCAL)
def list_invoices(
    status: Annotated[
        Literal["issued", "paid"] | None,
        Field(
            description="Filter by state: 'issued' = unpaid (includes overdue ones), 'paid' = paid. "
            "Omitted = all invoices, including cancelled ones and credit notes."
        ),
    ] = None,
    month: Annotated[
        str | None,
        Field(
            pattern=r"^\d{4}-\d{2}$",
            description="Only invoices issued in this month, format YYYY-MM, e.g. '2026-10'. Omitted = all months. "
            "Combines with status.",
        ),
    ] = None,
) -> str:
    """List issued invoices (and credit notes) from the local ledger, oldest first, one line each: number, issue
    date, customer, total incl. VAT, due date and state — unpaid ('nezaplacena'), overdue ('PO SPLATNOSTI'),
    paid with date, or cancelled ('STORNO' with reason).

    Read-only, no network. Use it to find unpaid/overdue invoices or an invoice number; for one invoice's items,
    bank details, QR string and PDF path use get_invoice. It does not pick up new PDFs — run sync_invoices
    first if invoices were added to the folder outside ucetka. Received invoices (expenses) are not included.
    Returns 'Žádné faktury.' when nothing matches.
    """
    records = ledger.list_records(status=status, month=month)
    if not records:
        return "Žádné faktury."
    return "\n".join(_record_summary(r) for r in records)


@mcp.tool(title="Show invoice detail", annotations=_READ_LOCAL)
def get_invoice(
    number: Annotated[
        str,
        Field(
            pattern=_INVOICE_NO_RE,
            description="Invoice or credit-note number exactly as in the ledger, e.g. '2610001' (see list_invoices).",
        ),
    ],
) -> str:
    """Show one issued invoice from the ledger: summary line with state, PDF path, customer, items, bank
    account with IBAN and the SPAYD string encoded in its QR payment code.

    Read-only, no network. Use it when the number is known; to search or filter use list_invoices. Fails with
    InvoiceNotFound for an unknown number. Invoices without a bank account (e.g. recorded via
    add_manual_invoice) show no IBAN/SPAYD.
    """
    r = ledger.get(number)
    inv = r.invoice
    acc = inv.payment.bank_account
    lines = [
        _record_summary(r),
        f"PDF: {r.pdf_path}",
        f"Odběratel: {_party_summary(inv.customer)}",
    ]
    lines += [
        f"  {ln.quantity} {ln.unit} × {_money(ln.unit_price_net, inv)} — {ln.description}"
        for ln in inv.lines
    ]
    if acc is None:
        lines.append("Bez bankovního účtu (placeno jinak než převodem).")
        return "\n".join(lines)
    lines.append(
        f"Účet {acc.as_string()} · IBAN {format_iban(account_to_iban(acc.prefix, acc.number, acc.bank_code))}"
    )
    lines.append(f"SPAYD: {make_spayd(inv, with_invoice=True)}")
    return "\n".join(lines)


@mcp.tool(
    title="Mark issued invoices paid",
    annotations=_hints(read_only=False, destructive=False, idempotent=True, open_world=False),
)
def mark_paid(
    numbers: Annotated[
        list[str],
        Field(min_length=1, description="One or more issued invoice numbers, e.g. ['2610001', '2610002']."),
    ],
    paid_date: Annotated[
        str | None,
        Field(
            description=f"Payment date {_ISO_DATE} applied to all listed invoices, or the literal 'due' to use each "
            "invoice's own due date. Omitted = today."
        ),
    ] = None,
) -> str:
    """Mark issued invoices (money received from customers) as paid on a given date, e.g. after checking a bank
    statement. Updates ~/.ucetka/invoices/<number>.json and returns the updated summary line of each invoice.

    Idempotent: repeating the call sets the same state; a different paid_date overwrites the stored one.
    Invoices are processed in order and the call stops at the first unknown number (earlier ones stay paid).
    It does not check the current state, so do not use it on a cancelled invoice. For received invoices
    (expenses) use mark_expense_paid; for tax or insurance payments to authorities use record_payment.
    """
    out = []
    for number in numbers:
        if paid_date == "due":
            day = ledger.get(number).invoice.due_date
        else:
            day = date.fromisoformat(paid_date) if paid_date else date.today()
        out.append(_record_summary(ledger.mark_paid(number, day)))
    return "\n".join(out)


@mcp.tool(
    title="Export invoice to Pohoda XML",
    annotations=_hints(read_only=False, destructive=False, idempotent=True, open_world=False),
)
def export_invoice_pohoda(
    number: Annotated[
        str,
        Field(pattern=_INVOICE_NO_RE, description="Issued invoice number from the ledger, e.g. '2610001'."),
    ],
    eu_classification: Annotated[
        str | None,
        Field(
            description="Pohoda VAT classification code (Nastavení → Členění DPH) for an EU service. Required "
            "when the invoice is reverse charge, ignored otherwise."
        ),
    ] = None,
) -> str:
    """Export one issued invoice as a Pohoda XML import file (windows-1250, validated against the Pohoda XSD),
    for an accountant who uses the Pohoda accounting software.

    Writes faktura_<number>.pohoda.xml next to the invoice PDF and overwrites it on repeat, so calling again is
    safe. No network. Fails for unknown numbers, credit notes (not supported), reverse-charge invoices without
    eu_classification, VAT rates other than 21/12/0, item texts too long for Pohoda or characters not
    representable in windows-1250. Returns the file path and the Pohoda import menu path. Not needed for the
    built-in reports (export_vat_return, export_book).
    """
    r = ledger.get(number)
    target = r.pdf_path.with_name(f"faktura_{number}.pohoda.xml")
    target.write_bytes(
        render_pohoda_xml(r.invoice, eu_classification=eu_classification)
    )
    return f"Pohoda XML: {target} — import v Pohodě: Soubor → Datová komunikace → XML import."


@mcp.tool(
    title="Draft invoice e-mail in Mail.app",
    annotations=_hints(read_only=False, destructive=False, idempotent=False, open_world=True),
)
def draft_invoice_email(
    number: Annotated[
        str,
        Field(pattern=_INVOICE_NO_RE, description="Issued invoice number from the ledger, e.g. '2610001'."),
    ],
    to: Annotated[
        str | None,
        Field(
            description="Recipient e-mail address. Omitted = the e-mail stored on the invoice's customer; if "
            "there is none, the draft has no recipient."
        ),
    ] = None,
    message: Annotated[
        str | None,
        Field(
            description="Full e-mail body to use instead of the default Czech text (which states the number, "
            "amount, due date, account, IBAN and variable symbol)."
        ),
    ] = None,
) -> str:
    """Open a DRAFT e-mail in Apple Mail.app with the invoice PDF attached, subject 'Faktura <number> —
    <supplier>', ready for the user to review and send. Never sends anything.

    macOS only (drives Mail.app via osascript and brings it to the front); fails elsewhere or if the PDF file is
    missing. Not idempotent: each call creates another draft. Use it right after issue_invoice or
    issue_credit_note; afterwards, when the money arrives, use mark_paid. Returns a confirmation with the
    recipient.
    """
    r = ledger.get(number)
    inv = r.invoice
    recipient = to or inv.customer.email
    acc = inv.payment.bank_account
    payment = (
        f"Platba: účet {acc.as_string()} "
        f"(IBAN {format_iban(account_to_iban(acc.prefix, acc.number, acc.bank_code))}), "
        f"variabilní symbol {inv.payment.variable_symbol}. "
        "Na faktuře je QR kód pro platbu a strojově čitelná data ISDOC.\n\n"
        if acc
        else "\n"
    )
    body = message or (
        "Dobrý den,\n\n"
        f"v příloze posílám fakturu č. {inv.number} na částku {_money(inv.total_gross, inv)} "
        f"se splatností {format_date(inv.due_date)}.\n"
        f"{payment}"
        f"S pozdravem\n{inv.supplier.name}"
    )
    mail.create_draft(
        recipient, f"Faktura {inv.number} — {inv.supplier.name}", body, r.pdf_path
    )
    who = recipient or "(bez adresáta — doplň v Mail.app)"
    return f"Koncept v Mail.app připraven pro {who}. Zkontroluj a odešli ručně."


def _kc(value: Decimal) -> str:
    return format_money(value).rsplit(",", 1)[0] + " Kč"


@mcp.tool(
    title="Export quarterly VAT filings",
    annotations=_hints(read_only=False, destructive=False, idempotent=False, open_world=False),
)
def export_vat_return(
    year: Annotated[int, Field(description="Calendar year of the VAT quarter, e.g. 2026.")],
    quarter: Annotated[
        int, Field(ge=1, le=4, description="VAT quarter 1–4 (1 = January–March, … 4 = October–December).")
    ],
    filed_on: Annotated[
        str | None,
        Field(description=f"Filing date {_ISO_DATE} written into the XML forms. Omitted = today."),
    ] = None,
    preview: Annotated[
        bool,
        Field(
            description="True = only compute and show the form rows, write no XML files (the import sync still "
            "runs). Default False."
        ),
    ] = False,
) -> str:
    """Prepare the quarterly VAT filings as EPO XML files: VAT return (DPHDP3), control statement (DPHKH1) and,
    only if there were EU services, the EC sales list (DPHSHV). Returns the computed form rows and file paths.

    Side effects: first runs the same import as sync_invoices (new PDFs are added to the ledger and the
    import-issue lists are rewritten), then writes into
    ~/Documents/Účetnictví/Přehledy/<year>/<year>_Q<n>_přehledy/{DPH,KH,SH}/. File names carry a timestamp, so
    every non-preview call adds a new set of files instead of overwriting. Nothing is filed: the user uploads the
    files on Moje daně (EPO). Requires a quarterly VAT payer and the `epo` section in ~/.ucetka/tax.yaml; fails
    on documents that cannot be classified (e.g. an EU supplier without DIČ, VAT ID). Resolve
    list_import_issues first. For a quick look at VAT due use tax_overview.
    """
    expenses_mod.sync()
    importer.sync()
    if preview:
        return "\n".join(dph.describe(dph.collect(year, quarter)))
    paths, ret = dph.export(
        year, quarter, filed_on=date.fromisoformat(filed_on) if filed_on else None
    )
    return "\n".join([*dph.describe(ret), *map(str, paths)])


@mcp.tool(
    title="Tax and income overview",
    annotations=_hints(read_only=False, destructive=False, idempotent=True, open_world=False),
)
def tax_overview(
    year: Annotated[
        int | None, Field(description="Calendar year to summarise, e.g. 2026. Omitted = current year.")
    ] = None,
    with_forecast: Annotated[
        bool,
        Field(
            description="True = add a projection to 31 December (income, income tax, social and health "
            "insurance, effective rate, limit usage). Default False."
        ),
    ] = False,
    method: Annotated[
        Literal["ytd", "3m"],
        Field(
            description="Forecast basis, used only with with_forecast: 'ytd' = average of all completed months "
            "this year (default), '3m' = average of the last 3 completed months."
        ),
    ] = "ytd",
) -> str:
    """Year-to-date business summary in text: invoiced and paid income, estimated income tax and social/health
    insurance vs. advances paid, VAT per period, effective tax rate, limits (flat-rate cap, 23 % bracket, VAT registration), overdue
    invoices, number of unprocessed import issues and the next deadlines.

    Not read-only: before computing it runs the same import as sync_invoices (new PDFs from the issued and
    received folders are added to the ledger, import-issue lists are rewritten); repeating is safe. No network,
    files nothing. Needs supplier config from `ucetka init`. Use it for "how am I doing / what do I owe";
    advance_schedule for the month-by-month ČSSZ (social insurance) advances, export_vat_return to produce VAT
    filings, export_book for the income/expense book, open_dashboard for a graphical view.
    """
    o = overview.build(year or date.today().year)
    out = [
        f"Rok {o.year} (k {format_date(o.today)}): fakturováno {_kc(o.issued_net)} bez DPH, zaplaceno {_kc(o.paid_net)}.",
        f"Odhad pro daň (zaplacené + letos vystavené nezaplacené): příjmy {_kc(o.expected_income)}, "
        f"zisk {_kc(o.tax.profit)}.",
    ]
    for e in o.estimates:
        out.append(
            f"• {e.label}: odhad {_kc(e.expected)}, zaplaceno {_kc(e.paid)}, plánované zálohy {_kc(e.planned)} "
            f"→ {'doplatek' if e.remaining > 0 else 'přeplatek'} {_kc(abs(e.remaining))}. {e.note}"
        )
    out.append(
        "DPH: "
        + "; ".join(
            f"{v.label} {_kc(v.result)} (do {format_date(v.due)})"
            for v in o.vat_periods
        )
    )
    eff = o.effective
    out.append(
        f"Efektivní zdanění (daň + pojištění {_kc(eff.burden)}): {eff.on_income:.1%} z příjmů, "
        f"{eff.on_profit:.1%} ze zisku."
    )
    fc = overview.forecast(o, method) if with_forecast else None
    if fc:
        out.append(
            f"Prognóza do 31. 12. ({fc.basis_months} měs. průměr {_kc(fc.run_rate)}/měs.): příjmy {_kc(fc.income)}, "
            f"daň {_kc(fc.tax.tax)}, sociální {_kc(fc.social.amount)}, zdravotní {_kc(fc.health.amount)} "
            f"→ efektivně {fc.effective.on_income:.1%} z příjmů."
        )
    for lim in overview.limits(o, fc, vat_payer=load_supplier().vat_payer):
        tail = f", prognóza {lim.forecast_share:.0%}" if lim.forecast is not None else ""
        out.append(f"{lim.label}: {lim.share:.0%} ({_kc(lim.actual)} z {_kc(lim.threshold)}{tail}).")
    if o.overdue:
        out.append(
            f"Po splatnosti {len(o.overdue)}: "
            + ", ".join(r.invoice.number for r in o.overdue[:10])
        )
    flags = len(o.invoice_issues) + len(o.expense_issues)
    if flags:
        out.append(f"⚠ {flags} dokladů k ručnímu zadání — viz list_import_issues.")
    out.append(
        "Termíny: "
        + "; ".join(f"{format_date(d.when)} {d.what}" for d in o.deadlines[:4])
    )
    return "\n".join(out)


@mcp.tool(
    title="Import new invoice PDFs",
    annotations=_hints(read_only=False, destructive=False, idempotent=True, open_world=False),
)
def sync_invoices() -> str:
    """Import new PDFs from the issued-invoices folder (read via the embedded ISDOC e-invoice) and the
    received-invoices folder (ISDOC, or Alza invoice text) into the ledger, for this and last year.

    Side effects: adds records under ~/.ucetka/invoices and ~/.ucetka/expenses, re-links moved PDFs and rewrites
    the issue lists (import_issues.json, expense_issues.json). Idempotent: already-imported PDFs are skipped, so
    repeating is safe. No network. Returns counts of imported issued/received documents and of documents needing
    manual entry. Then call list_import_issues and record each listed document with add_expense (received) or
    add_manual_invoice (issued). tax_overview and export_vat_return run this import automatically.
    """
    issued = importer.sync()
    received = expenses_mod.sync()
    flags = len(issued.issues) + len(expenses_mod.load_issues())
    hint = " — zpracuj je: list_import_issues, přečti PDF, zapiš přes add_expense / add_manual_invoice" if flags else ""
    return f"Vydané: +{len(issued.imported)} · přijaté: +{len(received)} · k zadání: {flags}{hint}"


@mcp.tool(title="List documents needing manual entry", annotations=_READ_LOCAL)
def list_import_issues() -> str:
    """List documents the last import could not process, one per line with the PDF path and the reason: no ISDOC,
    unknown format, foreign supplier, duplicate number, or a recorded document whose PDF has disappeared.

    Read-only: it shows the lists stored by the last sync and does not re-scan — call sync_invoices first for a
    fresh state. For each received ('Přijatá') document read the PDF and record it with add_expense (pass
    pdf_path); for each issued ('Vydaná') one use add_manual_invoice. If a received PDF was deleted on purpose,
    use remove_expense. Ask the user about unclear points (reverse charge, private use) instead of guessing.
    Returns 'Nic, vše načteno.' when there is nothing to do.
    """
    lines = [f"Vydaná {i.pdf_path}: {i.reason}" for i in importer.load_issues()]
    lines += [f"Přijatá {i.pdf_path}: {i.reason}" for i in expenses_mod.load_issues()]
    return "\n".join(lines) or "Nic, vše načteno."


@mcp.tool(
    title="Record received invoice (expense)",
    annotations=_hints(read_only=False, destructive=False, idempotent=False, open_world=False),
)
def add_expense(
    expense_id: Annotated[
        str,
        Field(
            pattern=_EXPENSE_ID_RE,
            description="Expense ID = PDF file name without extension (e.g. '2603002' for 2603002.pdf); letters, "
            "digits, '-' or '_'. Must not exist yet.",
        ),
    ],
    supplier_name: Annotated[str, Field(description="Supplier's name as on the invoice, e.g. 'Alza.cz a.s.'.")],
    document_number: Annotated[str, Field(description="The supplier's own invoice number, e.g. 'FV-2026-0042'.")],
    tax_point_date: Annotated[
        str,
        Field(description=f"DUZP (tax point date) {_ISO_DATE} from the invoice; decides the VAT period. Also stored as issue date."),
    ],
    base: Annotated[
        Decimal, Field(description="Tax base (amount without VAT) in CZK, e.g. 1000.00. Convert foreign currency first.")
    ],
    vat_rate: Annotated[
        Decimal,
        Field(description="VAT rate in percent: 21 (default), 12 or 0. Use 0 for documents without deductible VAT."),
    ] = Decimal(21),
    vat: Annotated[
        Decimal | None,
        Field(
            description="VAT amount in CZK exactly as on the document. Omitted = base × vat_rate / 100 rounded to "
            "0.01. Under reverse_charge it is the self-assessed VAT."
        ),
    ] = None,
    reverse_charge: Annotated[
        bool,
        Field(
            description="True for a foreign supplier charging no Czech VAT (e.g. Google, cloud/AI services): VAT "
            "is self-assessed on both output and input. Requires a non-CZ supplier_country; not combinable with "
            "claim_date. Default False."
        ),
    ] = False,
    supplier_dic: Annotated[
        str | None,
        Field(description="Supplier's DIČ / VAT ID, e.g. 'CZ12345678' or 'IE3668997OH'. Needed for EU suppliers in the control statement."),
    ] = None,
    supplier_country: Annotated[
        str | None,
        Field(
            pattern=r"^[A-Z]{2}$",
            description="ISO alpha-2 country of the supplier's REGISTERED SEAT, e.g. 'IE', 'PL', 'US'; required "
            "for reverse_charge. Take it from the address on the invoice, not from the DIČ prefix (a US company "
            "with an Irish VAT ID is 'US').",
        ),
    ] = None,
    goods: Annotated[
        bool,
        Field(
            description="Only with reverse_charge: True = goods acquired from the EU (VAT return line 3), "
            "False = service (default)."
        ),
    ] = False,
    claim_date: Annotated[
        str | None,
        Field(
            description=f"{_ISO_DATE}; set only when a domestic document arrived after the DUZP period was "
            "already filed — the VAT deduction is then claimed in this date's period (§ 73). Not before DUZP, "
            "not with reverse_charge. Omitted = DUZP period."
        ),
    ] = None,
    paid_date: Annotated[
        str | None,
        Field(
            description=f"Payment date {_ISO_DATE} if already paid. With actual-expense bookkeeping an expense "
            "counts only once paid. Omitted = unpaid (set later with mark_expense_paid)."
        ),
    ] = None,
    tax_deductible: Annotated[
        bool,
        Field(
            description="True (default) = deductible for income tax. For a non-deductible document (e.g. "
            "hospitality) pass False and vat_rate 0 so no VAT is deducted."
        ),
    ] = True,
    business_share: Annotated[
        int,
        Field(ge=1, le=100, description="Business-use share in percent, 1–100 (phone half private = 50). Default 100."),
    ] = 100,
    pdf_path: Annotated[
        str | None,
        Field(
            description="Absolute path to the document PDF, as shown by list_import_issues; links the record "
            "to the file and clears the issue. Omitted = record without a file."
        ),
    ] = None,
    note: Annotated[
        str | None, Field(description="Free-text note, shown as the subject in the income/expense book.")
    ] = None,
) -> str:
    """Record a received invoice (expense) by hand, mainly documents the import could not read (see
    list_import_issues). Returns a one-line confirmation with base, VAT and whether it is input VAT or
    reverse-charge self-assessment.

    Side effects: writes ~/.ucetka/expenses/<expense_id>.json, then re-runs the received-invoice import so the
    matching issue disappears. No network. Not idempotent: if expense_id already exists it fails and changes
    nothing — use remove_expense first to replace a wrong entry. Validation fails on inconsistent reverse-charge,
    goods or claim_date combinations. Single VAT rate per call. For issued invoices use add_manual_invoice; to
    mark an existing expense paid use mark_expense_paid.
    """
    duzp = date.fromisoformat(tax_point_date)
    vat_amount = (
        vat if vat is not None else (base * vat_rate / 100).quantize(Decimal("0.01"))
    )
    expense = expenses_mod.save(
        expenses_mod.Expense(
            id=expense_id,
            document_number=document_number,
            supplier_name=supplier_name,
            supplier_dic=supplier_dic,
            issue_date=duzp,
            tax_point_date=duzp,
            vat_rows=[expenses_mod.VatRow(rate=vat_rate, base=base, vat=vat_amount)],
            total=base if reverse_charge else base + vat_amount,
            supplier_country=supplier_country,
            reverse_charge=reverse_charge,
            goods=goods,
            claim_date=date.fromisoformat(claim_date) if claim_date else None,
            paid_date=date.fromisoformat(paid_date) if paid_date else None,
            tax_deductible=tax_deductible,
            business_share=business_share,
            note=note,
            pdf_path=Path(pdf_path) if pdf_path else None,
        )
    )
    expenses_mod.sync()
    kind = "samovyměření (výstup i vstup)" if reverse_charge else "vstup"
    return f"Zapsáno {expense.id}: {supplier_name}, základ {_kc(base)}, DPH {_kc(vat_amount)} — {kind}."


@mcp.tool(
    title="Mark expense paid",
    annotations=_hints(read_only=False, destructive=False, idempotent=False, open_world=False),
)
def mark_expense_paid(
    expense_id: Annotated[
        str,
        Field(
            pattern=_EXPENSE_ID_RE,
            description="ID of a recorded expense = its PDF name without extension, e.g. '2603002'.",
        ),
    ],
    paid_date: Annotated[
        str | None, Field(description=f"Date the expense was paid, {_ISO_DATE}. Omitted = today.")
    ] = None,
) -> str:
    """Mark a received invoice (expense) as paid. Matters for bookkeeping with actual expenses (daňová evidence),
    where an expense counts for income tax only once paid; flat-rate users rarely need it.

    Updates ~/.ucetka/expenses/<expense_id>.json. Not idempotent: fails if the expense is already marked paid
    (the stored date is kept) or does not exist. Returns a confirmation. For issued invoices paid by customers
    use mark_paid; for tax or insurance payments use record_payment; to set the date when first recording an
    expense pass paid_date to add_expense.
    """
    e = expenses_mod.mark_paid(expense_id, date.fromisoformat(paid_date) if paid_date else date.today())
    return f"Výdaj {e.id} ({e.supplier_name}) zaplacen {format_date(e.paid_date)}."


@mcp.tool(
    title="Export income/expense book",
    annotations=_hints(read_only=False, destructive=False, idempotent=True, open_world=False),
)
def export_book(
    year: Annotated[
        int | None, Field(description="Calendar year of the book, e.g. 2025. Omitted = current year.")
    ] = None,
) -> str:
    """Export the yearly income and expense book (kniha příjmů a výdajů, the cash-basis tax record) as CSV
    (semicolon-separated, opens in Czech Excel) and PDF. Rows are only PAID issued invoices and PAID expenses,
    dated by payment date. Returns income, deductible expenses, their difference and both file paths.

    Writes ~/Documents/Účetnictví/Přehledy/<year>/kniha_<year>.csv and .pdf, overwriting previous versions, so
    repeating is safe. Does not run the import (call sync_invoices first if new PDFs were added) and contacts no
    network. Needs supplier config from `ucetka init`. Use tax_overview for estimates of tax due and
    export_vat_return for VAT filings.
    """
    book = kniha.build(year or date.today().year)
    csv_path, pdf_path = kniha.write_csv(book), kniha.write_pdf(book)
    return (
        f"Příjmy {_kc(book.income)}, daňové výdaje {_kc(book.deductible_expenses)}, rozdíl {_kc(book.difference)}.\n"
        f"{csv_path}\n{pdf_path}"
    )


@mcp.tool(
    title="Record existing issued invoice",
    annotations=_hints(read_only=False, destructive=False, idempotent=False, open_world=False),
)
def add_manual_invoice(
    pdf_path: Annotated[
        str,
        Field(
            description="Absolute path of the existing issued-invoice PDF, as listed by list_import_issues. Not "
            "read or checked here; a missing file is flagged by the next sync."
        ),
    ],
    number: Annotated[
        str,
        Field(pattern=_INVOICE_NO_RE, description="Invoice number as printed on the PDF, e.g. '2603004'. Must be new."),
    ],
    issue_date: Annotated[str, Field(description=f"Issue date {_ISO_DATE}.")],
    due_date: Annotated[str, Field(description=f"Due date {_ISO_DATE}.")],
    customer_name: Annotated[str, Field(description="Customer's name as on the invoice.")],
    lines: Annotated[
        list[LineInput],
        Field(min_length=1, description="Invoice items as on the PDF (at least one), prices per unit without VAT."),
    ],
    tax_point_date: Annotated[
        str | None,
        Field(description=f"DUZP (tax point date) {_ISO_DATE}. Omitted = issue_date."),
    ] = None,
    customer_ico: Annotated[
        str | None,
        Field(
            pattern=r"^\d{6,10}$",
            description="Customer's IČO (company ID), 6–10 digits. Stored as given, no ARES lookup.",
        ),
    ] = None,
    customer_dic: Annotated[
        str | None,
        Field(description="Customer's DIČ (VAT ID), e.g. 'CZ12345678'. If given, the customer counts as a VAT payer."),
    ] = None,
    paid_date: Annotated[
        str | None,
        Field(description=f"Payment date {_ISO_DATE} if already paid. Omitted = unpaid (set later with mark_paid)."),
    ] = None,
) -> str:
    """Record in the ledger an issued invoice whose PDF already exists but which the import could not read (see
    list_import_issues). Nothing is generated or rendered — the PDF stays as it is. Returns a confirmation with
    the base and VAT.

    Writes ~/.ucetka/invoices/<number>.json (customer address left empty, no bank account) and re-runs the
    issued-invoice import. No network. Not idempotent: fails if the number already exists in the ledger. To
    create a NEW invoice with a PDF use issue_invoice; for received invoices use add_expense.
    """
    supplier = load_supplier().party()
    issued = date.fromisoformat(issue_date)
    default_rate = Decimal(21) if supplier.vat_payer else Decimal(0)
    inv = Invoice(
        number=number,
        issue_date=issued,
        tax_point_date=date.fromisoformat(tax_point_date) if tax_point_date else issued,
        due_date=date.fromisoformat(due_date),
        supplier=supplier,
        customer=Party(
            name=customer_name,
            ico=customer_ico,
            dic=customer_dic,
            vat_payer=bool(customer_dic),
            address=Address(street="", city="", zip=""),
        ),
        payment={"variable_symbol": number},
        lines=[
            {
                "quantity": ln.quantity,
                "unit": ln.unit,
                "description": ln.description,
                "unit_price_net": ln.unit_price,
                "vat_rate": default_rate if ln.vat_rate is None else ln.vat_rate,
            }
            for ln in lines
        ],
    )
    ledger.save(inv, Path(pdf_path), source="manual")
    if paid_date:
        ledger.mark_paid(number, date.fromisoformat(paid_date))
    importer.sync()
    return f"Zapsáno {number}: {customer_name}, základ {_kc(inv.total_net)}, DPH {_kc(inv.total_vat)}."


@mcp.tool(
    title="Record tax or insurance payment",
    annotations=_hints(read_only=False, destructive=False, idempotent=False, open_world=False),
)
def record_payment(
    kind: Annotated[
        Kind,
        Field(
            description="What was paid: 'dpfo' = personal income tax, 'cssz' = ČSSZ social insurance, "
            "'zp' = health insurance, 'dph' = VAT."
        ),
    ],
    amount: Annotated[Decimal, Field(gt=0, description="Amount paid in CZK, positive, e.g. 4759.")],
    paid_on: Annotated[
        str | None, Field(description=f"Date of the payment {_ISO_DATE}. Omitted = today.")
    ] = None,
    tax_year: Annotated[
        int | None,
        Field(
            description="Tax year the payment belongs to, e.g. 2025 for a settlement paid in 2026. Omitted = "
            "year of period if given, otherwise year of paid_on."
        ),
    ] = None,
    period: Annotated[
        str | None,
        Field(
            pattern=r"^\d{4}-\d{2}$",
            description="For a monthly advance: the month it covers, YYYY-MM, e.g. '2026-03'. Leave empty for "
            "a settlement (doplatek) or other one-off payment.",
        ),
    ] = None,
    note: Annotated[str | None, Field(description="Free-text note, e.g. 'doplatek za 2025' (settlement for 2025).")] = None,
) -> str:
    """Record a payment the user made to an authority — an advance (záloha) or settlement (doplatek) of income
    tax, ČSSZ (social insurance), health insurance or VAT. tax_overview and advance_schedule use these to show
    what is paid and what remains.

    Appends to ~/.ucetka/payments.json. Not idempotent: there is no duplicate check, so calling twice records the
    payment twice (there is no tool to delete one). Pays nothing and contacts no one. Returns a confirmation.
    For invoices paid by customers use mark_paid; for paid supplier invoices use mark_expense_paid.
    """
    day = date.fromisoformat(paid_on) if paid_on else date.today()
    p = store_payment(
        Payment(
            kind=kind,
            amount=amount,
            paid_on=day,
            tax_year=tax_year or (int(period[:4]) if period else day.year),
            period=period,
            note=note,
        )
    )
    return f"Zapsáno: {KIND_LABELS[p.kind]} {_kc(p.amount)} ({format_date(p.paid_on)}) za rok {p.tax_year}."


@mcp.tool(title="ČSSZ advance schedule", annotations=_READ_LOCAL)
def advance_schedule(
    year: Annotated[
        int | None, Field(description="Calendar year of the schedule, e.g. 2026. Omitted = current year.")
    ] = None,
) -> str:
    """Show the month-by-month ČSSZ (social insurance) advances for a year — each month paid (with amount),
    overdue, due this month or future — plus the estimated monthly advance after this year's annual statement
    and a note on income-tax advances.

    Read-only: uses the monthly amount from ~/.ucetka/tax.yaml and payments recorded with record_payment; does not
    run the import and contacts no network. Needs supplier config from `ucetka init`. Use record_payment to log
    an advance once paid, and tax_overview for the overall tax and insurance picture.
    """
    o = overview.build(year or date.today().year, sync=False)
    amount = o.settings.social_advance_monthly
    months = overview.advance_schedule(o.year, o.today, amount)
    labels = {"paid": "zaplaceno", "overdue": "nezapsáno, po splatnosti", "due": "splatné tento měsíc", "future": "budoucí"}
    out = [f"Zálohy ČSSZ {o.year}: {_kc(amount)} měsíčně, splatnost do konce měsíce."]
    out += [f"{m.period}: {labels[m.status]}" + (f" ({_kc(m.paid)})" if m.paid else "") for m in months]
    nxt = overview.next_year_advances(o, overview.forecast(o))
    out.append(f"Odhad zálohy ČSSZ od přehledu za {o.year}: {_kc(nxt.social_monthly)} měsíčně. {nxt.social_note}")
    out.append(f"Daň z příjmů: {nxt.dpfo_note}")
    return "\n".join(out)


@mcp.tool(
    title="Cancel issued invoice",
    annotations=_hints(read_only=False, destructive=True, idempotent=True, open_world=False),
)
def cancel_invoice(
    number: Annotated[
        str,
        Field(pattern=_INVOICE_NO_RE, description="Number of the issued invoice to cancel, e.g. '2610001'."),
    ],
    reason: Annotated[
        str,
        Field(description="Why it is cancelled, stored in the ledger, e.g. 'chybná, nahrazena fakturou 2609001'."),
    ],
) -> str:
    """Cancel (storno) an issued invoice: it stays in the ledger and in the number series, but no longer counts
    towards income, VAT or overviews. Use ONLY when the user explicitly cancels it and it was never delivered to
    the customer; if it was already delivered, do not cancel — issue a credit note with issue_credit_note.

    Destructive: overwrites the invoice's status (also a paid one) in ~/.ucetka/invoices/<number>.json; no tool
    restores it. Repeating sets the same state (a new reason replaces the old). The PDF file is kept. Re-runs the
    issued-invoice import afterwards. Fails for unknown numbers. Confirm with the user first.
    """
    r = ledger.cancel(number, reason)
    importer.sync()
    return f"Faktura {r.invoice.number} stornována: {reason}"


@mcp.tool(
    title="Issue credit note",
    annotations=_hints(read_only=False, destructive=False, idempotent=False, open_world=False),
)
def issue_credit_note(
    original_number: Annotated[
        str,
        Field(pattern=_INVOICE_NO_RE, description="Number of the issued invoice being corrected, e.g. '2610001'."),
    ],
    reason: Annotated[
        str,
        Field(description="Reason for the correction, printed on the document, e.g. 'sleva za zpoždění dodávky'."),
    ],
    lines: Annotated[
        list[LineInput] | None,
        Field(
            description="Partial credit only: the reductions as POSITIVE amounts (vat_rate omitted = the "
            "original's first-line rate). Omitted = credit the whole invoice."
        ),
    ] = None,
    issue_date: Annotated[
        str | None,
        Field(
            description=f"Credit note date = correction date and its DUZP, {_ISO_DATE}; not before the original's "
            "issue date. Omitted = today."
        ),
    ] = None,
) -> str:
    """Issue a credit note (opravný daňový doklad / dobropis) for an issued invoice: PDF/A-3 with ISDOC
    (DocumentType 2), no QR code, with the next number in the regular series. Returns the PDF path and the
    reduction (base, VAT, total to refund).

    Use it instead of cancel_invoice when the invoice was already delivered, or for a partial discount. Amounts
    on the document are positive; overviews and the VAT return subtract them in the period of the credit-note
    date. Side effects: writes ~/Documents/Účetnictví/Vydané faktury/<YY_MM_vydane>/dobropis_<number>.pdf and
    a ledger record. Not idempotent: each call creates another credit note, but the total credited can never
    exceed the original's base. Fails if the original is unknown, cancelled or itself a credit note. Currency and
    exchange rate are taken from the original (no network). Confirm with the user first; then
    draft_invoice_email can send it.
    """
    original = ledger.get(original_number).invoice
    rate = original.lines[0].vat_rate
    items = (
        [
            LineItem(
                quantity=ln.quantity,
                unit=ln.unit,
                description=ln.description,
                unit_price_net=ln.unit_price,
                vat_rate=rate if ln.vat_rate is None else ln.vat_rate,
            )
            for ln in lines
        ]
        if lines
        else None
    )
    note = build_credit_note(
        original_number,
        reason,
        lines=items,
        issue_date=date.fromisoformat(issue_date) if issue_date else None,
    )
    record = issue(note)
    return (
        f"Dobropis {note.number} k faktuře {original_number}: {record.pdf_path}\n"
        f"Snížení: základ {_money(note.total_net, note)} · DPH {_money(note.total_vat, note)} · "
        f"celkem {_money(note.total_gross, note)} (k vrácení odběrateli)."
    )


@mcp.tool(
    title="Remove expense record",
    annotations=_hints(read_only=False, destructive=True, idempotent=False, open_world=False),
)
def remove_expense(
    expense_id: Annotated[
        str,
        Field(
            pattern=_EXPENSE_ID_RE,
            description="ID of the recorded expense = its PDF name without extension, e.g. '2603002'.",
        ),
    ],
) -> str:
    """Delete a received invoice (expense) record from the ledger — only when the user explicitly deleted the
    document or does not want it recorded (e.g. list_import_issues reports its PDF as missing), or to replace a
    wrong add_expense entry.

    Destructive: deletes ~/.ucetka/expenses/<expense_id>.json with no undo; the PDF itself is not touched. It
    then re-runs the received-invoice import, so if the PDF is still in the received folder it is imported or
    flagged again right away. Fails if the ID does not exist. Confirm with the user first. Returns what was
    removed.
    """
    e = expenses_mod.remove(expense_id)
    expenses_mod.sync()
    return (
        f"Odebráno z evidence: {e.id} {e.supplier_name} (základ {_kc(e.base_total)})."
    )


DASHBOARD_URL = "http://127.0.0.1:8765/prehled"


@mcp.tool(
    title="Open dashboard in browser",
    annotations=_hints(read_only=True, destructive=False, idempotent=True, open_world=True),
)
def open_dashboard() -> str:
    """Open the graphical dashboard (charts of income, taxes and limits) at http://127.0.0.1:8765/prehled in the
    user's default browser. Returns the URL.

    macOS only (uses the `open` command); changes no data, but opens a new browser tab on every call. It does not
    start the web server — that runs as a background LaunchAgent set up by the installer (or `ucetka dashboard`);
    if it is not running the page will not load. Use tax_overview when a text answer is enough.
    """
    subprocess.run(["open", DASHBOARD_URL], check=True)
    return f"Otevřeno {DASHBOARD_URL}"


def main() -> None:
    importer.sync()
    expenses_mod.sync()
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
