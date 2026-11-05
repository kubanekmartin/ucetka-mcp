"""Kniha příjmů a výdajů pro daňovou evidenci — zaplacené faktury a výdaje za rok (CSV pro Excel + PDF)."""
from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from ucetka import expenses as expenses_mod
from ucetka import ledger
from ucetka.config import load_supplier, reports_dir
from ucetka.formatting import format_date, format_money

TEMPLATES_DIR = Path(__file__).resolve().parents[1] / "render" / "templates"
ZERO = Decimal(0)
HEADER = [
    "Datum úhrady", "Doklad", "Partner", "Předmět", "Typ", "Celkem vč. DPH", "DPH",
    "Základ pro daň z příjmů", "Daňový", "Podíl podnikání %",
]


@dataclass
class BookRow:
    paid: date
    document: str
    partner: str
    subject: str
    kind: str
    total: Decimal
    vat: Decimal
    tax_amount: Decimal
    deductible: bool
    share: int


@dataclass
class Book:
    year: int
    rows: list[BookRow]

    @property
    def income(self) -> Decimal:
        return sum((r.tax_amount for r in self.rows if r.kind == "příjem"), ZERO)

    @property
    def deductible_expenses(self) -> Decimal:
        return sum((r.tax_amount for r in self.rows if r.kind == "výdaj" and r.deductible), ZERO)

    @property
    def non_deductible_expenses(self) -> Decimal:
        return sum((r.total for r in self.rows if r.kind == "výdaj" and not r.deductible), ZERO)

    @property
    def difference(self) -> Decimal:
        return self.income - self.deductible_expenses


def build(year: int) -> Book:
    vat_payer = load_supplier().vat_payer
    rows = []
    for r in ledger.list_records():
        if r.status != "paid" or not r.paid_date or r.paid_date.year != year:
            continue
        inv = r.invoice
        rows.append(BookRow(
            r.paid_date, inv.number, inv.customer.name, inv.lines[0].description,
            "příjem", inv.sign * (inv.total_net_czk + inv.total_vat_czk), inv.sign * inv.total_vat_czk,
            inv.sign * inv.total_net_czk, True, 100,
        ))
    for e in expenses_mod.list_expenses():
        if not e.paid_date or e.paid_date.year != year:
            continue
        rows.append(BookRow(
            e.paid_date, e.document_number, e.supplier_name, e.note or "", "výdaj", e.total,
            ZERO if e.reverse_charge else e.vat_total,
            expenses_mod.income_tax_cost(e, vat_payer=vat_payer), e.tax_deductible, e.business_share,
        ))
    return Book(year, sorted(rows, key=lambda r: (r.paid, r.kind, r.document)))


def _folder(year: int) -> Path:
    folder = reports_dir() / str(year)
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def write_csv(book: Book) -> Path:
    path = _folder(book.year) / f"kniha_{book.year}.csv"
    # utf-8-sig + středník: Excel v české lokalizaci to otevře správně bez importu.
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(HEADER)
        for r in book.rows:
            w.writerow([
                r.paid.isoformat(), r.document, r.partner, r.subject, r.kind,
                format_money(r.total), format_money(r.vat), format_money(r.tax_amount),
                "ano" if r.deductible else "ne", r.share,
            ])
    return path


def write_pdf(book: Book) -> Path:
    from weasyprint import HTML  # lazy: dlopen pango/cairo

    env = Environment(loader=FileSystemLoader(TEMPLATES_DIR), autoescape=select_autoescape(["html"]))
    html = env.get_template("kniha.html.j2").render(
        book=book, supplier=load_supplier(), money=format_money, fmt_date=format_date, today=date.today()
    )
    path = _folder(book.year) / f"kniha_{book.year}.pdf"
    path.write_bytes(HTML(string=html).write_pdf())
    return path
