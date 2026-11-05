from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Annotated

import typer
import yaml

from ucetka import ledger
from ucetka.config import load_supplier
from ucetka.customers import CustomerNotFound, get_customer
from ucetka import expenses as expenses_mod
from ucetka.export import dph, kniha
from ucetka.export.pohoda import render_pohoda_xml
from ucetka.isdoc.generator import render_isdoc
from ucetka.isdoc.validator import IsdocValidationError, validate
from ucetka.issue import build_credit_note
from ucetka.issue import issue as issue_invoice
from ucetka.models import BankAccount, Invoice, Party

app = typer.Typer(
    add_completion=False,
    help="Generátor českých faktur (PDF + embedded ISDOC + QR Platba).",
    no_args_is_help=True,
)



def _load_yaml(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def _build_invoice(
    supplier_path: Path | None,
    invoice_path: Path,
    *,
    customers_path: Path | None = None,
    auto_number: bool = False,
) -> Invoice:
    supplier_cfg = load_supplier(supplier_path)
    supplier = supplier_cfg.party()
    data = _load_yaml(invoice_path)
    if "supplier" in data:
        raise typer.BadParameter(
            "Invoice YAML nesmí obsahovat 'supplier' — dodavatel se načítá ze --supplier."
        )
    data["supplier"] = supplier.model_dump()

    if "customer_id" in data:
        if "customer" in data:
            raise typer.BadParameter("Použij buď 'customer:' nebo 'customer_id:', ne obojí.")
        cid = data.pop("customer_id")
        try:
            cust = get_customer(cid, path=customers_path)
        except CustomerNotFound as exc:
            raise typer.BadParameter(str(exc)) from exc
        data["customer"] = cust.model_dump()

    if supplier_cfg.bank_account is not None:
        data.setdefault("payment", {}).setdefault("bank_account", supplier_cfg.bank_account.model_dump())
    if auto_number or not data.get("number"):
        data["number"] = ledger.next_number(data["issue_date"])
    data.setdefault("payment", {}).setdefault("variable_symbol", data["number"])

    return Invoice.model_validate(data)


@app.command()
def issue(
    invoice: Annotated[Path, typer.Option("--invoice", "-i", help="YAML s daty faktury (odběratel, položky, platba).", exists=True, dir_okay=False, readable=True)],
    out: Annotated[Path | None, typer.Option("--out", "-o", help="Výstupní adresář (default ~/Documents/faktury).", file_okay=False)] = None,
    supplier: Annotated[Path | None, typer.Option("--supplier", "-s", help="YAML s dodavatelem.", exists=True, dir_okay=False, readable=True)] = None,
    customers: Annotated[Path | None, typer.Option("--customers", help="YAML s adresářem příjemců pro 'customer_id' lookup.", dir_okay=False, readable=True)] = None,
    auto_number: Annotated[bool, typer.Option("--auto-number", help="Ignorovat 'number:' v YAML a vzít další číslo z evidence.")] = False,
    no_qr: Annotated[bool, typer.Option("--no-qr", help="Negenerovat QR Platba.")] = False,
    no_isdoc: Annotated[bool, typer.Option("--no-isdoc", help="Negenerovat / nepřikládat ISDOC XML.")] = False,
    qr_plus_f: Annotated[bool, typer.Option("--qr-plus-f/--qr-basic", help="QR Faktura (+F) s embeddovaným SIND vs. základní QR Platba.")] = True,
    pdf_a3: Annotated[bool, typer.Option("--pdf-a3/--plain-pdf", help="PDF/A-3a, které ISDOC.PDF vyžaduje (default), nebo obyčejné PDF.")] = True,
) -> None:
    """Vystaví fakturu (PDF + embedded ISDOC + QR) ze dvou YAML vstupů a zapíše ji do evidence."""
    inv = _build_invoice(supplier, invoice, customers_path=customers, auto_number=auto_number)
    try:
        record = issue_invoice(
            inv, out_dir=out, qr=not no_qr, qr_plus_f=qr_plus_f, isdoc=not no_isdoc, pdf_a3=pdf_a3
        )
    except IsdocValidationError as exc:
        typer.echo(f"ISDOC nevalidní:\n{exc}", err=True)
        raise typer.Exit(code=2) from exc
    typer.echo(f"Vystaveno: {record.pdf_path}  ({record.pdf_path.stat().st_size} B)")


@app.command()
def validate_xml(
    invoice: Annotated[Path, typer.Option("--invoice", "-i", help="YAML s daty faktury.", exists=True, dir_okay=False, readable=True)],
    supplier: Annotated[Path | None, typer.Option("--supplier", "-s", help="YAML s dodavatelem.", exists=True, dir_okay=False, readable=True)] = None,
) -> None:
    """Vygeneruje ISDOC XML a zvaliduje ho proti XSD 6.0.2."""
    inv = _build_invoice(supplier, invoice)
    xml = render_isdoc(inv)
    try:
        validate(xml)
    except IsdocValidationError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2) from exc
    typer.echo(f"OK — ISDOC validní ({len(xml)} B)")


@app.command()
def next_num() -> None:
    """Vypíše příští číslo faktury pro dnešní měsíc (podle evidence)."""
    typer.echo(ledger.next_number(date.today()))


@app.command("list")
def list_cmd(
    status: Annotated[str | None, typer.Option("--status", help="issued / paid")] = None,
    month: Annotated[str | None, typer.Option("--month", help="YYYY-MM")] = None,
) -> None:
    """Vypíše vydané faktury z evidence."""
    for r in ledger.list_records(status=status, month=month):
        flag = "PO SPLATNOSTI" if r.overdue else r.status
        typer.echo(
            f"{r.invoice.number}  {r.invoice.issue_date}  {r.invoice.customer.name:<30}  "
            f"{r.invoice.total_gross:>12} {r.invoice.currency}  {flag}"
        )


@app.command()
def dashboard(
    port: Annotated[int, typer.Option("--port", help="Port lokálního serveru.")] = 8765,
    open_browser: Annotated[bool, typer.Option("--open/--no-open", help="Otevřít v prohlížeči.")] = True,
) -> None:
    """Spustí webový přehled (příjmy, daně, DPH, nezaplacené faktury)."""
    import webbrowser

    import uvicorn

    if open_browser:
        webbrowser.open(f"http://127.0.0.1:{port}/prehled")
    uvicorn.run("ucetka.web.app:app", host="127.0.0.1", port=port)


def _prompt_company(default: str | None) -> Party:
    from ucetka import ares

    while True:
        ico = typer.prompt("IČO", default=default).strip()
        try:
            return ares.lookup(ico)
        except ares.CompanyNotFound as exc:
            typer.echo(f"  ✗ {exc} — zkontroluj překlep a zkus to znovu.")
            default = None


def _prompt_account() -> BankAccount:
    while True:
        text = typer.prompt("Číslo účtu pro faktury ([předčíslí-]číslo/kód banky, např. 19-2000145399/0800)")
        try:
            return BankAccount.parse(text.strip())
        except ValueError as exc:
            typer.echo(f"  ✗ {exc.errors()[0]['msg'] if hasattr(exc, 'errors') else exc} — zkus to znovu.")


@app.command()
def init(
    ico: Annotated[str | None, typer.Option("--ico", help="Předvyplněné IČO.")] = None,
    actual_default: Annotated[
        bool, typer.Option("--skutecne-vydaje", help="Výchozí odpověď: daňová evidence se skutečnými výdaji.")
    ] = False,
    employee_default: Annotated[
        bool, typer.Option("--zamestnanec", help="Výchozí odpověď: zároveň zaměstnanec.")
    ] = False,
) -> None:
    """Průvodce prvním spuštěním: IČO z ARES, účet, DPH, daňový profil → supplier.yaml + tax.yaml."""
    from ucetka import onboarding

    party = _prompt_company(ico)
    typer.echo(f"ARES: {party.name}, {party.address.street}, {party.address.zip} {party.address.city}"
               f"{', DIČ ' + party.dic if party.dic else ''}{' (plátce DPH)' if party.vat_payer else ''}")
    if not typer.confirm("Sedí to?", default=True):
        raise typer.Exit(1)
    vat_payer = typer.confirm("Jsi plátce DPH?", default=party.vat_payer)
    party = party.model_copy(update={"vat_payer": vat_payer})
    account = _prompt_account()
    email = typer.prompt("E-mail na faktury (prázdné = žádný)", default="", show_default=False)
    phone = typer.prompt("Telefon na faktury (prázdné = žádný)", default="", show_default=False)
    party = party.model_copy(update={"email": email or None, "phone": phone or None})
    vat_period = (
        typer.prompt("Období DPH (quarterly = čtvrtletí, monthly = měsíc)", default="quarterly")
        if vat_payer else "quarterly"
    )
    actual = typer.confirm("Vedeš daňovou evidenci se skutečnými výdaji? (ne = výdajový paušál)", default=actual_default)
    flat = 60 if actual else int(typer.prompt("Paušál v % (30/40/60/80)", default="60"))
    employee = typer.confirm("Jsi zároveň zaměstnanec? (ne = student / pojištěnec státu)", default=employee_default)
    employment = Decimal(typer.prompt("Roční hrubá mzda v Kč (pro odhad daně)", default="0")) if employee else Decimal(0)
    social = Decimal(typer.prompt("Měsíční záloha na sociální (ČSSZ) v Kč", default="0"))
    written = onboarding.write_profile(
        party, account, vat_period=vat_period, expense_mode="actual" if actual else "flat_rate",
        flat_rate_percent=flat, health_status="employee" if employee else "student",
        employment_income=employment, social_advance_monthly=social,
    )
    typer.echo("Hotovo:\n  " + "\n  ".join(map(str, written)))
    typer.echo("Před prvním exportem DPH doplň do tax.yaml sekci epo: (finanční úřad) — viz README.")


@app.command()
def paid(
    number: Annotated[str, typer.Argument(help="Číslo faktury.")],
    on: Annotated[str | None, typer.Option("--on", help="Datum úhrady YYYY-MM-DD (default dnes).")] = None,
) -> None:
    """Označí fakturu jako zaplacenou."""
    record = ledger.mark_paid(number, date.fromisoformat(on) if on else date.today())
    typer.echo(f"{number} zaplacena {record.paid_date}")


@app.command()
def export_pohoda(
    invoice: Annotated[Path, typer.Option("--invoice", "-i", exists=True, dir_okay=False, readable=True)],
    out: Annotated[Path, typer.Option("--out", "-o", dir_okay=False)],
    supplier: Annotated[Path | None, typer.Option("--supplier", "-s", exists=True, dir_okay=False, readable=True)] = None,
    customers: Annotated[Path | None, typer.Option("--customers", dir_okay=False, readable=True)] = None,
    eu_classification: Annotated[str | None, typer.Option("--eu-classification", help="Zkratka členění DPH z Pohody pro službu do EU (povinné u reverse charge).")] = None,
) -> None:
    """Exportuje fakturu do Pohoda XML (Stormware schema version_2)."""
    inv = _build_invoice(supplier, invoice, customers_path=customers)
    xml = render_pohoda_xml(inv, eu_classification=eu_classification)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(xml)
    typer.echo(f"Pohoda XML: {out}  ({len(xml)} B, windows-1250)")


@app.command()
def dobropis(
    original: Annotated[str, typer.Argument(help="Číslo opravované faktury.")],
    reason: Annotated[str, typer.Option("--duvod", help="Důvod opravy.")],
    on: Annotated[str | None, typer.Option("--datum", help="Datum vystavení YYYY-MM-DD (default dnes).")] = None,
) -> None:
    """Vystaví dobropis celé faktury (částečný dobropis přes MCP issue_credit_note)."""
    record = issue_invoice(build_credit_note(original, reason, issue_date=date.fromisoformat(on) if on else None))
    typer.echo(f"Dobropis {record.invoice.number}: {record.pdf_path}")


@app.command("vydaj-zaplacen")
def expense_paid(
    expense_id: Annotated[str, typer.Argument(help="ID výdaje (název PDF bez přípony).")],
    on: Annotated[str | None, typer.Option("--on", help="Datum úhrady YYYY-MM-DD (default dnes).")] = None,
) -> None:
    """Označí přijatou fakturu jako zaplacenou (daňová evidence)."""
    e = expenses_mod.mark_paid(expense_id, date.fromisoformat(on) if on else date.today())
    typer.echo(f"{e.id} zaplacen {e.paid_date}")


@app.command("kniha")
def kniha_cmd(
    year: Annotated[int, typer.Option("--rok", help="Rok.")] = date.today().year,
) -> None:
    """Kniha příjmů a výdajů jako CSV + PDF (Přehledy/<rok>/kniha_<rok>.*)."""
    book = kniha.build(year)
    typer.echo(f"Příjmy {book.income} · daňové výdaje {book.deductible_expenses} · rozdíl {book.difference}")
    typer.echo(kniha.write_csv(book))
    typer.echo(kniha.write_pdf(book))


@app.command("dph")
def dph_cmd(
    year: Annotated[int, typer.Option("--rok", help="Rok.")],
    quarter: Annotated[int, typer.Option("--q", help="Čtvrtletí 1–4.")],
    filed_on: Annotated[str | None, typer.Option("--datum", help="Datum podání YYYY-MM-DD (default dnes).")] = None,
) -> None:
    """Přiznání k DPH + kontrolní hlášení (+ souhrnné hlášení) jako XML pro EPO (Přehledy/<rok>/<rok>_Q<n>_přehledy)."""
    paths, ret = dph.export(year, quarter, filed_on=date.fromisoformat(filed_on) if filed_on else None)
    typer.echo("\n".join([*dph.describe(ret), *map(str, paths)]))


if __name__ == "__main__":
    app(prog_name="ucetka")
