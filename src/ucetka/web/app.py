"""Minimální FastAPI web UI nad ucetka.

Spuštění:
    DYLD_FALLBACK_LIBRARY_PATH=/opt/homebrew/lib \\
      uv run uvicorn ucetka.web.app:app --reload

Endpointy:
    GET  /                  — formulář (HTML)
    GET  /prehled           — dashboard: příjmy, efektivní zdanění, graf (+ prognóza), termíny, nezaplacené faktury
    GET  /dane              — skutečnost vs. prognóza do konce roku, limity, odhad vs. zálohy
    GET  /zalohy            — rozpis záloh ČSSZ po měsících, QR platba, záloha na příští rok, ostatní platby
    GET  /vydaje            — přijaté faktury (výdaje), úhrady, kniha příjmů a výdajů (POST /vydaje/kniha)
    GET  /dph               — DPH po čtvrtletích + export XML pro EPO (POST /dph/export)
    GET  /faktury           — seznam faktur z evidence s filtry a upozorněním na splatnost
    POST /faktury/zaplaceno — označí fakturu jako zaplacenou
    POST /issue             — vystaví PDF z form data, vrátí jako download
    POST /api/issue         — JSON API: dict → PDF bytes
"""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from io import BytesIO
from pathlib import Path
from typing import Literal
from urllib.parse import quote

from fastapi import FastAPI, Form, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from jinja2 import Environment, FileSystemLoader, select_autoescape

from ucetka import expenses as expenses_mod
from ucetka import ledger, overview
from ucetka.config import load_supplier
from ucetka.customers import load_customers
from ucetka.formatting import format_date, format_money, format_money_with_currency
from ucetka.isdoc.validator import IsdocValidationError
from ucetka.issue import issue
from ucetka.export import dph, kniha
from ucetka.models import BankAccount, Invoice, Party, SupplierConfig
from ucetka.payments import KIND_LABELS, Kind, Payment, load_payments, load_settings, record_payment
from ucetka.render.qr import make_payment_qr_data_uri

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
STATIC_DIR = Path(__file__).resolve().parent / "static"
DUE_SOON_DAYS = 7

app = FastAPI(title="ucetka web")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

_env = Environment(
    loader=FileSystemLoader(TEMPLATES_DIR),
    autoescape=select_autoescape(["html"]),
    trim_blocks=True,
    lstrip_blocks=True,
)


def _load_supplier() -> SupplierConfig:
    try:
        return load_supplier()
    except FileNotFoundError as exc:
        raise HTTPException(500, str(exc)) from exc


def _kc(value: Decimal) -> str:
    return format_money(value).rsplit(",", 1)[0] + " Kč"


def _days(n: int) -> str:
    if n == 1:
        return "1 den"
    if 2 <= n <= 4:
        return f"{n} dny"
    return f"{n} dní"


def _gross_czk(r: ledger.InvoiceRecord) -> Decimal:
    return r.invoice.sign * (r.invoice.total_net_czk + r.invoice.total_vat_czk)


def _status(r: ledger.InvoiceRecord, today: date) -> tuple[str, str]:
    if r.status == "cancelled":
        return "cancelled", "Stornováno"
    if r.invoice.is_credit_note:
        return ("paid", "Dobropis vrácen") if r.status == "paid" else ("issued", "Dobropis k vrácení")
    if r.status == "paid":
        return "paid", "Zaplaceno"
    if r.invoice.due_date < today:
        return "overdue", f"Po splatnosti {_days((today - r.invoice.due_date).days)}"
    return "issued", "Nezaplaceno"


def _reminders(today: date) -> dict:
    unpaid = [r for r in ledger.list_records(status="issued") if not r.invoice.is_credit_note]
    overdue = sorted((r for r in unpaid if r.invoice.due_date < today), key=lambda r: r.invoice.due_date)
    soon = sorted(
        (r for r in unpaid if today <= r.invoice.due_date <= today + timedelta(days=DUE_SOON_DAYS)),
        key=lambda r: r.invoice.due_date,
    )
    return {
        "overdue": [(r, (today - r.invoice.due_date).days) for r in overdue],
        "soon": [(r, (r.invoice.due_date - today).days) for r in soon],
        "overdue_total": sum((_gross_czk(r) for r in overdue), Decimal(0)),
    }


_env.globals.update(
    kc=_kc,
    fmt_czk=lambda v: format_money_with_currency(v, "Kč"),
    fmt_cur=format_money_with_currency,
    fmt_date=format_date,
    days=_days,
)


def _render(template: str, active: str, **ctx) -> str:
    today = date.today()
    reminders = _reminders(today)
    ctx.setdefault("supplier", _load_supplier())
    return _env.get_template(template).render(
        active=active, today_date=today, reminders=reminders, nav_overdue=len(reminders["overdue"]), **ctx
    )


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    supplier = _load_supplier()
    customers = load_customers()
    return _render(
        "index.html",
        "new",
        supplier=supplier,
        customers=customers,
        suggested_number=ledger.next_number(date.today()),
        today=date.today().isoformat(),
        due_default=(date.today() + timedelta(days=14)).isoformat(),
    )


def _decimal(text: str) -> Decimal:
    """Formulář bere česky psaná čísla: „1 250,50“."""
    try:
        return Decimal(text.replace("\u00a0", "").replace(" ", "").replace(",", "."))
    except InvalidOperation as exc:
        raise HTTPException(400, f"„{text}“ není číslo") from exc


@app.post("/issue")
def issue_form(
    customer_id: str = Form(""),
    customer_name: str = Form(""),
    customer_ico: str = Form(""),
    customer_dic: str = Form(""),
    customer_street: str = Form(""),
    customer_city: str = Form(""),
    customer_zip: str = Form(""),
    number: str = Form(""),
    issue_date: str = Form(...),
    tax_point_date: str = Form(""),
    due_date: str = Form(...),
    item_description: list[str] = Form(...),
    item_quantity: list[str] | None = Form(None),
    item_unit: list[str] | None = Form(None),
    item_unit_price_net: list[str] = Form(...),
    item_vat_rate: list[str] = Form(...),
    bank_account: str = Form(""),
    variable_symbol: str = Form(""),
    qr_plus_f: bool = Form(True),
    pdf_a3: bool = Form(True),
    language: Literal["cs", "en"] = Form("cs"),
):
    supplier_cfg = _load_supplier()
    supplier = supplier_cfg.party()

    if customer_id:
        customers = load_customers()
        if customer_id not in customers:
            raise HTTPException(400, f"Neznámý customer_id: {customer_id}")
        customer = customers[customer_id]
    else:
        if not customer_name:
            raise HTTPException(400, "Vyplň customer_id nebo customer_name.")
        customer = Party(
            name=customer_name,
            ico=customer_ico or None,
            dic=customer_dic or None,
            vat_payer=bool(customer_dic),
            address={
                "street": customer_street,
                "city": customer_city,
                "zip": customer_zip,
            },
        )

    if bank_account:
        if "/" not in bank_account:
            raise HTTPException(400, "bank_account musí být ve formátu '[prefix-]cislo/banka' (např. 2000145399/0100).")
        acc_part, bank_code = bank_account.rsplit("/", 1)
        prefix, num = acc_part.split("-", 1) if "-" in acc_part else (None, acc_part)
        account = {"prefix": prefix, "number": num, "bank_code": bank_code}
    elif supplier_cfg.bank_account is not None:
        account = supplier_cfg.bank_account.model_dump()
    else:
        raise HTTPException(400, "Chybí bank_account a supplier.yaml nemá výchozí účet.")

    issued = date.fromisoformat(issue_date)
    final_number = number or ledger.next_number(issued)
    inv = Invoice(
        number=final_number,
        issue_date=issued,
        tax_point_date=date.fromisoformat(tax_point_date or issue_date),
        due_date=date.fromisoformat(due_date),
        supplier=supplier,
        customer=customer,
        payment={"bank_account": account, "variable_symbol": variable_symbol or final_number},
        lines=[
            {
                "quantity": _decimal(qty),
                "unit": unit.strip() or "ks",
                "description": desc,
                "unit_price_net": _decimal(price),
                "vat_rate": Decimal(rate),
            }
            for desc, qty, unit, price, rate in zip(
                item_description,
                item_quantity or ["1"] * len(item_description),
                item_unit or ["ks"] * len(item_description),
                item_unit_price_net,
                item_vat_rate,
                strict=True,
            )
        ],
        issued_by=supplier.name,
        language=language,
    )

    try:
        record = issue(inv, qr_plus_f=qr_plus_f, pdf_a3=pdf_a3)
    except IsdocValidationError as exc:
        raise HTTPException(500, f"ISDOC nevalidní: {exc}") from exc
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    pdf = record.pdf_path.read_bytes()

    return StreamingResponse(
        BytesIO(pdf),
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="faktura_{inv.number}.pdf"'},
    )


def _years(today: date) -> list[int]:
    return sorted({today.year - 1, today.year})


def _next_vat(o: overview.YearOverview) -> overview.VatPeriod:
    return next((v for v in o.vat_periods if v.due >= o.today), o.vat_periods[-1])


@app.get("/prehled", response_class=HTMLResponse)
def prehled(year: int | None = None) -> str:
    today = date.today()
    o = overview.build(year or today.year, today)
    fc = overview.forecast(o)
    forecast_months = (
        [None] * (today.month - 1) + [float(max(fc.run_rate - o.months[today.month - 1].issued_net, 0))]
        + [float(fc.run_rate)] * (12 - today.month)
        if fc else []
    )
    return _render(
        "prehled.html",
        "overview",
        o=o,
        fc=fc,
        years=_years(today),
        next_vat=_next_vat(o),
        chart={
            "current": [float(m.issued_net) for m in o.months],
            "previous": [float(m.previous_year_issued_net) for m in o.months],
            "forecast": forecast_months,
        },
    )


@app.get("/dane", response_class=HTMLResponse)
def dane(year: int | None = None, metoda: Literal["ytd", "3m"] = "ytd") -> str:
    today = date.today()
    o = overview.build(year or today.year, today, sync=False)
    fc = overview.forecast(o, metoda)
    return _render(
        "dane.html",
        "taxes",
        o=o,
        fc=fc,
        method=metoda,
        years=_years(today),
        limits=overview.limits(o, fc, vat_payer=_load_supplier().vat_payer),
        nxt=overview.next_year_advances(o, fc),
    )


@app.get("/zalohy", response_class=HTMLResponse)
def zalohy(year: int | None = None, ulozeno: str | None = None) -> str:
    today = date.today()
    o = overview.build(year or today.year, today, sync=False)
    amount = o.settings.social_advance_monthly
    months = overview.advance_schedule(o.year, today, amount) if amount else []
    to_pay = next((m for m in months if m.status in ("overdue", "due")), None)
    pay = o.settings.cssz_payment
    qr = (
        make_payment_qr_data_uri(
            BankAccount.parse(pay.account), to_pay.amount, pay.variable_symbol, f"Zaloha OSVC {to_pay.period}"
        )
        if pay and to_pay
        else None
    )
    return _render(
        "zalohy.html",
        "advances",
        o=o,
        months=months,
        to_pay=to_pay,
        qr=qr,
        pay=pay,
        years=_years(today),
        nxt=overview.next_year_advances(o, overview.forecast(o)),
        kinds=KIND_LABELS,
        payments=sorted(load_payments(o.year), key=lambda p: p.paid_on, reverse=True),
        just_saved=ulozeno,
    )


@app.post("/zalohy/zaplaceno")
def zalohy_paid(
    period: str = Form(...), amount: Decimal = Form(...), paid_on: str = Form(...)
) -> RedirectResponse:
    record_payment(Payment(
        kind="cssz", amount=amount, paid_on=date.fromisoformat(paid_on), tax_year=int(period[:4]), period=period,
    ))
    return RedirectResponse(f"/zalohy?year={period[:4]}&ulozeno={period}", status_code=303)


@app.post("/zalohy/dosud")
def zalohy_paid_until_now(year: int = Form(...)) -> RedirectResponse:
    today = date.today()
    o = overview.build(year, today, sync=False)
    for m in overview.advance_schedule(year, today, o.settings.social_advance_monthly):
        if m.status == "overdue":
            record_payment(Payment(
                kind="cssz", amount=m.amount, paid_on=m.due, tax_year=year, period=m.period,
                note="zapsáno hromadně, datum = splatnost",
            ))
    return RedirectResponse(f"/zalohy?year={year}&ulozeno=vse", status_code=303)


@app.post("/zalohy/platba")
def zalohy_payment(
    kind: Kind = Form(...),
    amount: Decimal = Form(...),
    paid_on: str = Form(...),
    tax_year: int = Form(...),
    note: str = Form(""),
) -> RedirectResponse:
    record_payment(
        Payment(kind=kind, amount=amount, paid_on=date.fromisoformat(paid_on), tax_year=tax_year, note=note or None)
    )
    return RedirectResponse(f"/zalohy?year={tax_year}&ulozeno=platba", status_code=303)


@app.get("/vydaje", response_class=HTMLResponse)
def vydaje(year: int | None = None, kniha_out: str | None = None) -> str:
    today = date.today()
    year = year or today.year
    vat_payer = _load_supplier().vat_payer
    items = [e for e in expenses_mod.list_expenses() if e.issue_date.year == year or (e.paid_date and e.paid_date.year == year)]
    rows = [{"e": e, "cost": expenses_mod.income_tax_cost(e, vat_payer=vat_payer)} for e in reversed(items)]
    return _render(
        "vydaje.html", "expenses", rows=rows, year=year, years=_years(today), today=today.isoformat(),
        mode=load_settings().expense_mode,
        unpaid=[r for r in rows if not r["e"].paid_date],
        paid_cost=sum((r["cost"] for r in rows if r["e"].paid_date and r["e"].paid_date.year == year), Decimal(0)),
        exported=kniha_out.split("|") if kniha_out else [],
    )


@app.post("/vydaje/zaplaceno")
def vydaje_paid(expense_id: str = Form(...), paid_on: str = Form(...), year: int = Form(...)) -> RedirectResponse:
    try:
        expenses_mod.mark_paid(expense_id, date.fromisoformat(paid_on))
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return RedirectResponse(f"/vydaje?year={year}", status_code=303)


@app.get("/vydaj/{expense_id}.pdf")
def expense_pdf(expense_id: str) -> FileResponse:
    match = [e for e in expenses_mod.list_expenses() if e.id == expense_id and e.pdf_path]
    if not match or not match[0].pdf_path.exists():
        raise HTTPException(404, f"PDF výdaje {expense_id} není")
    return FileResponse(match[0].pdf_path, media_type="application/pdf")


@app.post("/vydaje/kniha")
def vydaje_book(year: int = Form(...)) -> RedirectResponse:
    book = kniha.build(year)
    paths = [kniha.write_csv(book), kniha.write_pdf(book)]
    return RedirectResponse(f"/vydaje?year={year}&kniha_out={quote('|'.join(map(str, paths)))}", status_code=303)


@app.get("/dph", response_class=HTMLResponse)
def dph_page(year: int | None = None, export: str | None = None) -> str:
    today = date.today()
    o = overview.build(year or today.year, today, sync=False)
    return _render(
        "dph.html", "vat", o=o, years=_years(today), next_vat=_next_vat(o),
        exported=export.split("|") if export else [],
    )


@app.post("/dph/export")
def dph_export(year: int = Form(...), quarter: int = Form(...)) -> RedirectResponse:
    try:
        paths, _ = dph.export(year, quarter)
    except dph.VatExportError as exc:
        raise HTTPException(400, str(exc)) from exc
    return RedirectResponse(
        f"/dph?year={year}&export={quote('|'.join(str(p) for p in paths))}", status_code=303
    )


@app.get("/faktury", response_class=HTMLResponse)
def faktury(zaplaceno: str | None = None) -> str:
    today = date.today()
    records = sorted(
        ledger.list_records(), key=lambda r: (r.invoice.issue_date, r.invoice.number), reverse=True
    )
    rows = []
    for r in records:
        state, label = _status(r, today)
        rows.append({"r": r, "state": state, "label": label, "net": r.invoice.sign * r.invoice.total_net_czk, "gross": _gross_czk(r)})
    unpaid = [row for row in rows if row["state"] in ("issued", "overdue")]
    return _render(
        "faktury.html",
        "invoices",
        rows=rows,
        years=sorted({r.invoice.issue_date.year for r in records}, reverse=True),
        months=sorted({r.invoice.issue_date.strftime("%Y-%m") for r in records}, reverse=True),
        unpaid_count=len(unpaid),
        unpaid_total=sum((row["gross"] for row in unpaid), Decimal(0)),
        paid_this_year=sum(
            (row["net"] for row in rows if row["state"] == "paid" and row["r"].paid_date and row["r"].paid_date.year == today.year),
            Decimal(0),
        ),
        today=today.isoformat(),
        just_paid=zaplaceno,
    )


@app.post("/faktury/zaplaceno")
def faktury_paid(number: str = Form(...), paid_on: str = Form(...)) -> RedirectResponse:
    try:
        ledger.mark_paid(number, date.fromisoformat(paid_on))
    except ledger.InvoiceNotFound as exc:
        raise HTTPException(404, str(exc)) from exc
    return RedirectResponse(f"/faktury?zaplaceno={number}", status_code=303)


@app.post("/prehled/zaplaceno")
def prehled_paid(number: str = Form(...), paid_on: str = Form(...), year: int = Form(...)) -> RedirectResponse:
    ledger.mark_paid(number, date.fromisoformat(paid_on))
    return RedirectResponse(f"/prehled?year={year}", status_code=303)


@app.get("/faktura/{number}.pdf")
def invoice_pdf(number: str) -> FileResponse:
    try:
        record = ledger.get(number)
    except ledger.InvoiceNotFound as exc:
        raise HTTPException(404, str(exc)) from exc
    return FileResponse(record.pdf_path, media_type="application/pdf")
