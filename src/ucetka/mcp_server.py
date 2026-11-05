"""MCP server pro ucetka — fakturace z Claude chatu. Registraci do Claude Desktop dělá install.sh (viz README)."""

from __future__ import annotations

import subprocess
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Literal

from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel, Field

from ucetka import ares, importer, ledger, mail, overview
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

mcp = FastMCP("ucetka")


class LineInput(BaseModel):
    description: str = Field(description="Popis položky, např. 'Přednáška o AI'.")
    unit_price: Decimal = Field(
        gt=0, description="Jednotková cena BEZ DPH v měně faktury."
    )
    quantity: Decimal = Field(
        Decimal(1), gt=0, description="Počet jednotek (např. hodin)."
    )
    unit: str = Field("ks", description="Jednotka: 'ks', 'h', 'měsíc'…")
    vat_rate: Decimal | None = Field(
        None,
        description="Sazba DPH v % (21 / 12 / 0). Prázdné = 21 pro plátce, 0 pro neplátce a reverse charge.",
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


@mcp.tool()
def get_supplier() -> str:
    """Dodavatel (fakturující) ze supplier.yaml včetně výchozího účtu."""
    s = load_supplier()
    lines = [f"Dodavatel: {_party_summary(s.party())}"]
    if s.bank_account:
        lines.append(f"Výchozí účet: {s.bank_account.as_string()}")
    return "\n".join(lines)


@mcp.tool()
def list_customers() -> str:
    """Adresář odběratelů (customers.yaml). customer_id použij v issue_invoice."""
    book = load_customers()
    if not book:
        return f"Adresář je prázdný ({customers_file()}). Přidej odběratele přes add_customer."
    return "\n".join(
        f"• {cid}: {_party_summary(p)}{f' · {p.email}' if p.email else ''}"
        for cid, p in book.items()
    )


@mcp.tool()
def lookup_company(
    ico: Annotated[str, Field(description="IČO firmy (8 číslic).")],
) -> str:
    """Najde firmu v ARES podle IČO — název, sídlo, DIČ, registrace k DPH."""
    return _party_summary(ares.lookup(ico))


@mcp.tool()
def add_customer(
    customer_id: Annotated[
        str, Field(description="Krátký klíč do adresáře, např. 'vzorova-firma'.")
    ],
    ico: Annotated[str, Field(description="IČO — údaje se doplní z ARES.")],
    email: Annotated[
        str | None, Field(description="E-mail pro zasílání faktur.")
    ] = None,
) -> str:
    """Přidá odběratele do adresáře podle IČO (data z ARES). Existující záznam nepřepíše."""
    party = add_customer_to_book(customer_id, ares.lookup(ico).model_copy(update={"email": email}))
    return f"Přidáno {customer_id}: {_party_summary(party)}"


@mcp.tool()
def peek_invoice_number(
    issue_date: Annotated[
        str | None, Field(description="Datum vystavení YYYY-MM-DD. Default dnes.")
    ] = None,
) -> str:
    """Příští číslo faktury podle evidence (nic nerezervuje)."""
    return ledger.next_number(
        date.fromisoformat(issue_date) if issue_date else date.today()
    )


@mcp.tool()
def issue_invoice(
    lines: Annotated[
        list[LineInput], Field(min_length=1, description="Položky faktury.")
    ],
    customer_id: Annotated[
        str | None, Field(description="Klíč z adresáře (list_customers).")
    ] = None,
    customer_ico: Annotated[
        str | None,
        Field(description="IČO odběratele mimo adresář — údaje se vezmou z ARES."),
    ] = None,
    customer_name: Annotated[
        str | None, Field(description="Odběratel bez IČO (např. zahraniční firma).")
    ] = None,
    customer_dic: Annotated[
        str | None, Field(description="DIČ / EU VAT ID odběratele bez IČO.")
    ] = None,
    customer_street: Annotated[
        str | None, Field(description="Ulice a číslo odběratele bez IČO.")
    ] = None,
    customer_city: Annotated[
        str | None, Field(description="Město odběratele bez IČO.")
    ] = None,
    customer_zip: Annotated[
        str | None, Field(description="PSČ odběratele bez IČO.")
    ] = None,
    customer_country: Annotated[
        str | None, Field(description="Název země odběratele bez IČO, např. 'Německo'.")
    ] = None,
    customer_country_code: Annotated[
        str, Field(description="ISO kód země odběratele bez IČO.")
    ] = "CZ",
    issue_date: Annotated[
        str | None, Field(description="Datum vystavení YYYY-MM-DD. Default dnes.")
    ] = None,
    tax_point_date: Annotated[
        str | None, Field(description="DUZP YYYY-MM-DD. Default = datum vystavení.")
    ] = None,
    due_days: Annotated[
        int, Field(ge=0, description="Splatnost ve dnech od vystavení.")
    ] = 14,
    currency: Annotated[
        str,
        Field(description="Měna faktury (CZK, EUR…). Kurz ČNB k DUZP se doplní sám."),
    ] = "CZK",
    reverse_charge: Annotated[
        bool,
        Field(description="Služba plátci DPH v jiném státě EU — daň odvede zákazník."),
    ] = False,
    note: Annotated[str | None, Field(description="Poznámka na faktuře.")] = None,
    bank_account: Annotated[
        str | None, Field(description="Jiný než výchozí účet '[předčíslí-]číslo/kód'.")
    ] = None,
    invoice_number: Annotated[
        str | int | None, Field(description="Vynucené číslo. Default další z evidence.")
    ] = None,
    language: Annotated[
        Literal["cs", "en"], Field(description="Jazyk PDF faktury: cs (default) nebo en.")
    ] = "cs",
) -> str:
    """Vystaví fakturu: PDF/A-3a s ISDOC 6.0.2 a QR Platbou+F, uloží ji do evidence a vrátí shrnutí.

    Před vystavením si ověř odběratele (list_customers / lookup_company) a částky s uživatelem.
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


@mcp.tool()
def list_invoices(
    status: Annotated[
        Literal["issued", "paid"] | None,
        Field(description="issued = nezaplacené, paid = zaplacené."),
    ] = None,
    month: Annotated[str | None, Field(description="Měsíc vystavení YYYY-MM.")] = None,
) -> str:
    """Vydané faktury z evidence, včetně těch po splatnosti."""
    records = ledger.list_records(status=status, month=month)
    if not records:
        return "Žádné faktury."
    return "\n".join(_record_summary(r) for r in records)


@mcp.tool()
def get_invoice(number: Annotated[str, Field(description="Číslo faktury.")]) -> str:
    """Detail faktury z evidence: položky, platební údaje, SPAYD řetězec QR kódu a cesta k PDF."""
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


@mcp.tool()
def mark_paid(
    numbers: Annotated[list[str], Field(min_length=1, description="Čísla faktur.")],
    paid_date: Annotated[
        str | None,
        Field(
            description="Datum úhrady YYYY-MM-DD. Default dnes; 'due' = ke dni splatnosti každé faktury."
        ),
    ] = None,
) -> str:
    """Označí faktury jako zaplacené (např. podle výpisu z banky)."""
    out = []
    for number in numbers:
        if paid_date == "due":
            day = ledger.get(number).invoice.due_date
        else:
            day = date.fromisoformat(paid_date) if paid_date else date.today()
        out.append(_record_summary(ledger.mark_paid(number, day)))
    return "\n".join(out)


@mcp.tool()
def export_invoice_pohoda(
    number: Annotated[str, Field(description="Číslo faktury z evidence.")],
    eu_classification: Annotated[
        str | None,
        Field(
            description="Zkratka členění DPH z Pohody pro službu do EU — povinné u reverse charge."
        ),
    ] = None,
) -> str:
    """Uloží fakturu jako Pohoda XML (windows-1250, validováno XSD) vedle PDF."""
    r = ledger.get(number)
    target = r.pdf_path.with_name(f"faktura_{number}.pohoda.xml")
    target.write_bytes(
        render_pohoda_xml(r.invoice, eu_classification=eu_classification)
    )
    return f"Pohoda XML: {target} — import v Pohodě: Soubor → Datová komunikace → XML import."


@mcp.tool()
def draft_invoice_email(
    number: Annotated[str, Field(description="Číslo faktury z evidence.")],
    to: Annotated[
        str | None, Field(description="Adresát. Default e-mail odběratele z adresáře.")
    ] = None,
    message: Annotated[
        str | None, Field(description="Vlastní text e-mailu místo výchozího.")
    ] = None,
) -> str:
    """Otevře v Mail.app KONCEPT e-mailu s PDF fakturou v příloze. Nic neodesílá — odeslání je na uživateli."""
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


@mcp.tool()
def export_vat_return(
    year: Annotated[int, Field(description="Rok.")],
    quarter: Annotated[int, Field(description="Čtvrtletí 1–4.")],
    filed_on: Annotated[
        str | None, Field(description="Datum podání YYYY-MM-DD. Default dnes.")
    ] = None,
    preview: Annotated[
        bool, Field(description="True = jen spočítat a ukázat, nic neukládat.")
    ] = False,
) -> str:
    """Přiznání k DPH (DPHDP3), kontrolní hlášení (DPHKH1) a při službách do EU i souhrnné hlášení (DPHSHV) pro EPO.

    Uloží do ~/Documents/Účetnictví/Přehledy/<rok>/<rok>_Q<n>_přehledy/{DPH,KH,SH}/. Nic nepodává —
    soubory se nahrají ručně na Moje daně (EPO → Načíst soubor → Kontrola → Odeslat).
    """
    expenses_mod.sync()
    importer.sync()
    if preview:
        return "\n".join(dph.describe(dph.collect(year, quarter)))
    paths, ret = dph.export(
        year, quarter, filed_on=date.fromisoformat(filed_on) if filed_on else None
    )
    return "\n".join([*dph.describe(ret), *map(str, paths)])


@mcp.tool()
def tax_overview(
    year: Annotated[int | None, Field(description="Rok. Default letošní.")] = None,
    with_forecast: Annotated[
        bool, Field(description="Přidat prognózu do konce roku (příjmy, daně, pojištění, limity).")
    ] = False,
    method: Annotated[
        Literal["ytd", "3m"],
        Field(description="Prognóza z průměru všech dokončených měsíců (ytd) nebo posledních 3 (3m)."),
    ] = "ytd",
) -> str:
    """Jak jsem na tom: příjmy, odhad daně a pojištění vs. zálohy, DPH za období, nezaplacené faktury, flagy, termíny.

    Nejdřív automaticky načte nové faktury ze složek Vydané/Přijaté faktury. Grafický přehled: `ucetka dashboard`.
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


@mcp.tool()
def sync_invoices() -> str:
    """Načte nové PDF z Vydaných faktur (ISDOC) a Přijatých faktur (ISDOC, jinak Alza) do evidence.

    Zbylé doklady hned zpracuj: list_import_issues → přečti PDF → add_expense / add_manual_invoice.
    """
    issued = importer.sync()
    received = expenses_mod.sync()
    flags = len(issued.issues) + len(expenses_mod.load_issues())
    hint = " — zpracuj je: list_import_issues, přečti PDF, zapiš přes add_expense / add_manual_invoice" if flags else ""
    return f"Vydané: +{len(issued.imported)} · přijaté: +{len(received)} · k zadání: {flags}{hint}"


@mcp.tool()
def list_import_issues() -> str:
    """Doklady, které import nepřečetl (bez ISDOC, cizí formát, zahraniční dodavatel).

    U každého přečti PDF z uvedené cesty, údaje (dodavatel, číslo, DUZP, rekapitulace DPH, země sídla) zapiš přes
    add_expense s pdf_path, resp. add_manual_invoice u vydaných. Nejasné věci (reverse charge, soukromé použití)
    se zeptej uživatele, nehádej.
    """
    lines = [f"Vydaná {i.pdf_path}: {i.reason}" for i in importer.load_issues()]
    lines += [f"Přijatá {i.pdf_path}: {i.reason}" for i in expenses_mod.load_issues()]
    return "\n".join(lines) or "Nic, vše načteno."


@mcp.tool()
def add_expense(
    expense_id: Annotated[
        str, Field(description="ID dokladu = název PDF bez přípony (např. '2603002').")
    ],
    supplier_name: Annotated[str, Field(description="Dodavatel.")],
    document_number: Annotated[str, Field(description="Číslo faktury dodavatele.")],
    tax_point_date: Annotated[str, Field(description="DUZP YYYY-MM-DD.")],
    base: Annotated[Decimal, Field(description="Základ (bez DPH) v Kč.")],
    vat_rate: Annotated[
        Decimal, Field(description="Sazba DPH v % (21/12/0).")
    ] = Decimal(21),
    vat: Annotated[
        Decimal | None,
        Field(description="DPH v Kč dle dokladu. Prázdné = dopočítat ze základu."),
    ] = None,
    reverse_charge: Annotated[
        bool,
        Field(
            description="Zahraniční služba bez české DPH (Google, OpenAI…) — DPH se samovyměří."
        ),
    ] = False,
    supplier_dic: Annotated[
        str | None, Field(description="DIČ / VAT ID dodavatele.")
    ] = None,
    supplier_country: Annotated[
        str | None,
        Field(
            description="Země SÍDLA dodavatele (ISO, např. IE, PL, US) — u reverse charge povinné. "
            "Podle adresy na faktuře, ne podle prefixu DIČ (Anthropic, PBC s irským DIČ = US)."
        ),
    ] = None,
    goods: Annotated[
        bool, Field(description="Reverse charge: pořízení zboží z EU (ř. 3), jinak služba.")
    ] = False,
    claim_date: Annotated[
        str | None,
        Field(
            description="YYYY-MM-DD: doklad dorazil až po podání období DUZP → odpočet uplatnit v období tohoto data (§ 73)."
        ),
    ] = None,
    paid_date: Annotated[
        str | None,
        Field(description="Datum úhrady YYYY-MM-DD — při skutečných výdajích se výdaj počítá až zaplacený."),
    ] = None,
    tax_deductible: Annotated[
        bool,
        Field(description="Daňově uznatelný pro daň z příjmů. Nedaňový doklad (pohoštění…) zadej s vat_rate 0, aby DPH nešlo do odpočtu."),
    ] = True,
    business_share: Annotated[
        int, Field(ge=1, le=100, description="Podíl použití pro podnikání v % (telefon napůl soukromě = 50).")
    ] = 100,
    pdf_path: Annotated[
        str | None,
        Field(description="Cesta k PDF (u flagovaných dokladů z list_import_issues)."),
    ] = None,
    note: Annotated[str | None, Field(description="Poznámka.")] = None,
) -> str:
    """Ručně zapíše přijatou fakturu (výdaj) — hlavně doklady, které import neumí přečíst."""
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


@mcp.tool()
def mark_expense_paid(
    expense_id: Annotated[str, Field(description="ID výdaje (název PDF bez přípony).")],
    paid_date: Annotated[str | None, Field(description="Datum úhrady YYYY-MM-DD. Default dnes.")] = None,
) -> str:
    """Označí přijatou fakturu (výdaj) jako zaplacenou — pro daňovou evidenci se skutečnými výdaji."""
    e = expenses_mod.mark_paid(expense_id, date.fromisoformat(paid_date) if paid_date else date.today())
    return f"Výdaj {e.id} ({e.supplier_name}) zaplacen {format_date(e.paid_date)}."


@mcp.tool()
def export_book(
    year: Annotated[int | None, Field(description="Rok. Default letošní.")] = None,
) -> str:
    """Kniha příjmů a výdajů (daňová evidence) za rok jako CSV + PDF do složky Přehledy/<rok>/."""
    book = kniha.build(year or date.today().year)
    csv_path, pdf_path = kniha.write_csv(book), kniha.write_pdf(book)
    return (
        f"Příjmy {_kc(book.income)}, daňové výdaje {_kc(book.deductible_expenses)}, rozdíl {_kc(book.difference)}.\n"
        f"{csv_path}\n{pdf_path}"
    )


@mcp.tool()
def add_manual_invoice(
    pdf_path: Annotated[
        str,
        Field(
            description="Cesta k existujícímu PDF vydané faktury (z list_import_issues)."
        ),
    ],
    number: Annotated[str, Field(description="Číslo faktury.")],
    issue_date: Annotated[str, Field(description="Datum vystavení YYYY-MM-DD.")],
    due_date: Annotated[str, Field(description="Splatnost YYYY-MM-DD.")],
    customer_name: Annotated[str, Field(description="Odběratel.")],
    lines: Annotated[
        list[LineInput], Field(min_length=1, description="Položky (ceny bez DPH).")
    ],
    tax_point_date: Annotated[
        str | None, Field(description="DUZP. Default = vystavení.")
    ] = None,
    customer_ico: Annotated[str | None, Field(description="IČO odběratele.")] = None,
    customer_dic: Annotated[str | None, Field(description="DIČ odběratele.")] = None,
    paid_date: Annotated[
        str | None, Field(description="Datum úhrady, pokud už je zaplacená.")
    ] = None,
) -> str:
    """Zapíše do evidence vydanou fakturu, kterou import nepřečetl (PDF už existuje, nic se negeneruje)."""
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


@mcp.tool()
def record_payment(
    kind: Annotated[
        Kind,
        Field(
            description="dpfo = daň z příjmů, cssz = sociální, zp = zdravotní, dph = DPH."
        ),
    ],
    amount: Annotated[Decimal, Field(gt=0, description="Zaplacená částka v Kč.")],
    paid_on: Annotated[
        str | None, Field(description="Datum platby YYYY-MM-DD. Default dnes.")
    ] = None,
    tax_year: Annotated[
        int | None, Field(description="Za který rok (default rok platby).")
    ] = None,
    period: Annotated[
        str | None,
        Field(description="Měsíc, za který je měsíční záloha, YYYY-MM. U doplatků nevyplňovat."),
    ] = None,
    note: Annotated[str | None, Field(description="Např. 'doplatek za 2025'.")] = None,
) -> str:
    """Zapíše zaplacenou zálohu / doplatek (daň, sociální, zdravotní, DPH)."""
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


@mcp.tool()
def advance_schedule(
    year: Annotated[int | None, Field(description="Rok. Default letošní.")] = None,
) -> str:
    """Rozpis měsíčních záloh ČSSZ: které měsíce jsou zaplacené / po splatnosti, a odhad zálohy na příští rok."""
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


@mcp.tool()
def cancel_invoice(
    number: Annotated[str, Field(description="Číslo vydané faktury.")],
    reason: Annotated[
        str, Field(description="Důvod, např. 'chybná, nahrazena fakturou 2609001'.")
    ],
) -> str:
    """Stornuje vydanou fakturu (zůstane v evidenci, nepočítá se). Jen pokud ji uživatel výslovně ruší.

    Pokud už byla doručena odběrateli, nestornuj — vystav dobropis přes issue_credit_note.
    """
    r = ledger.cancel(number, reason)
    importer.sync()
    return f"Faktura {r.invoice.number} stornována: {reason}"


@mcp.tool()
def issue_credit_note(
    original_number: Annotated[str, Field(description="Číslo opravované faktury z evidence.")],
    reason: Annotated[
        str, Field(description="Důvod opravy (tiskne se na doklad), např. 'sleva za zpoždění dodávky'.")
    ],
    lines: Annotated[
        list[LineInput] | None,
        Field(
            description="Jen částečný dobropis: položky snížení v KLADNÝCH částkách. Prázdné = dobropis celé faktury."
        ),
    ] = None,
    issue_date: Annotated[
        str | None, Field(description="Datum vystavení = datum opravy YYYY-MM-DD. Default dnes.")
    ] = None,
) -> str:
    """Vystaví opravný daňový doklad (dobropis) k vydané faktuře — PDF/A-3 s ISDOC (DocumentType 2), bez QR.

    Použij místo storna, když už faktura odešla odběrateli. Částky na dobropisu jsou kladné, v přehledech
    a v přiznání DPH se odečítají v období data opravy.
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


@mcp.tool()
def remove_expense(
    expense_id: Annotated[str, Field(description="ID výdaje (název PDF bez přípony).")],
) -> str:
    """Odebere přijatou fakturu z evidence — jen když ji uživatel výslovně smazal / nechce ji evidovat."""
    e = expenses_mod.remove(expense_id)
    expenses_mod.sync()
    return (
        f"Odebráno z evidence: {e.id} {e.supplier_name} (základ {_kc(e.base_total)})."
    )


DASHBOARD_URL = "http://127.0.0.1:8765/prehled"


@mcp.tool()
def open_dashboard() -> str:
    """Otevře grafický přehled (dashboard) v prohlížeči. Běží jako služba na pozadí (LaunchAgent)."""
    subprocess.run(["open", DASHBOARD_URL], check=True)
    return f"Otevřeno {DASHBOARD_URL}"


def main() -> None:
    importer.sync()
    expenses_mod.sync()
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
