"""Přijaté faktury (výdaje) — pro odpočet DPH. Paušalista je pro daň z příjmů nepotřebuje.

Pořadí: ISDOC příloha v PDF (jakýkoli tuzemský dodavatel) → text faktury od Alzy → flag v expense_issues.json,
který AI zpracuje sama: přečte PDF a zapíše ho přes MCP add_expense.
"""

from __future__ import annotations

import json
import re
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Literal

import pdfplumber
from pydantic import BaseModel, ConfigDict, Field, model_validator

from ucetka import importer
from ucetka.config import (
    expenses_dir,
    home_dir,
    load_supplier,
    received_dir,
    supplier_file,
)


class VatRow(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rate: Decimal
    base: Decimal
    vat: Decimal


class Expense(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,40}$")
    document_number: str
    supplier_name: str
    supplier_ico: str | None = None
    supplier_dic: str | None = None
    issue_date: date
    tax_point_date: date
    vat_rows: list[VatRow]
    total: Decimal
    supplier_country: str | None = Field(
        None,
        pattern=r"^[A-Z]{2}$",
        description="Země sídla dodavatele (ISO 3166). Rozhoduje o řádku přiznání, ne prefix DIČ — "
        "americké firmy mívají irské nebo EU OSS DIČ.",
    )
    reverse_charge: bool = Field(
        False,
        description="Služba od zahraničního dodavatele — DPH se samovyměří (na výstupu i na vstupu).",
    )
    goods: bool = Field(
        False, description="Pořízení zboží z EU (ř. 3), jinak přijetí služby."
    )
    claim_date: date | None = Field(
        None,
        description="Odpočet uplatnit v období tohoto data (§ 73 — doklad došel později). "
        "KH dál uvádí původní DUZP. Prázdné = období DUZP.",
    )
    paid_date: date | None = Field(
        None,
        description="Datum úhrady — daňová evidence (skutečné výdaje) počítá výdaj až zaplacený",
    )
    tax_deductible: bool = Field(
        True, description="Daňově uznatelný výdaj pro daň z příjmů"
    )
    business_share: int = Field(
        100,
        ge=1,
        le=100,
        description="Podíl použití pro podnikání v % (zbytek je soukromé použití)",
    )
    note: str | None = None
    pdf_path: Path | None = None
    source: Literal["imported", "manual"] = "manual"

    @model_validator(mode="after")
    def _reverse_charge_needs_country(self) -> Expense:
        if self.reverse_charge and self.supplier_country in (None, "CZ"):
            raise ValueError(
                "Reverse charge vyžaduje zahraniční supplier_country (např. IE, US)"
            )
        if self.claim_date and self.reverse_charge:
            raise ValueError(
                "claim_date jen u tuzemských dokladů — reverse charge se přiznává v období DUZP"
            )
        if self.claim_date and self.claim_date < self.tax_point_date:
            raise ValueError("claim_date nemůže být před DUZP")
        if self.goods and not self.reverse_charge:
            raise ValueError(
                "goods má smysl jen u reverse charge (pořízení zboží z EU)"
            )
        return self

    @property
    def vat_date(self) -> date:
        """Datum, podle kterého výdaj spadá do období DPH."""
        return self.claim_date or self.tax_point_date

    @property
    def base_total(self) -> Decimal:
        return sum((r.base for r in self.vat_rows), Decimal(0))

    @property
    def vat_total(self) -> Decimal:
        return sum((r.vat for r in self.vat_rows), Decimal(0))


class ExpenseIssue(BaseModel):
    pdf_path: Path
    reason: str
    detected_at: datetime


class ParseFailed(Exception):
    pass


_MONEY = r"-?\d{1,3}(?: \d{3})*,\d{2}"


def _money(text: str) -> Decimal:
    return Decimal(text.replace(" ", "").replace(",", "."))


def _cz_date(text: str) -> date:
    d, m, y = text.split(".")
    return date(int(y), int(m), int(d))


def _find(pattern: str, text: str, what: str) -> re.Match:
    m = re.search(pattern, text)
    if not m:
        raise ParseFailed(f"Nenašel jsem {what}")
    return m


def parse_alza(text: str, expense_id: str) -> Expense:
    if "Alza.cz a.s." not in text:
        raise ParseFailed(f"PDF bez ISDOC a není od Alzy — {NEEDS_AI}")
    number = _find(
        r"Faktura - (?:Daňový doklad - )?(\S+)", text, "číslo dokladu"
    ).group(1)
    ico, dic = _find(
        r"I[ČC]: (\d{8}), DI[ČC]: (CZ\d+)", text, "IČ/DIČ prodávajícího"
    ).groups()
    issued = _cz_date(
        _find(r"Datum vystavení: (\d{2}\.\d{2}\.\d{4})", text, "datum vystavení").group(
            1
        )
    )
    duzp = re.search(r"Datum uskut\. zdaň\. plnění: (\d{2}\.\d{2}\.\d{4})", text)
    rows: dict[Decimal, VatRow] = {}
    for m in re.finditer(rf"^(\d{{1,2}}) % ({_MONEY}) ({_MONEY})$", text, re.MULTILINE):
        rate = Decimal(m.group(1))
        # Rekapitulace DPH bývá u Alzy dvakrát (v dokladu a v Kč) — první výskyt stačí.
        rows.setdefault(
            rate, VatRow(rate=rate, base=_money(m.group(2)), vat=_money(m.group(3)))
        )
    if not rows:
        raise ParseFailed("Nenašel jsem rekapitulaci DPH")
    rounding = re.search(rf"Zaokrouhlení: ({_MONEY}) Kč", text)
    total = sum((r.base + r.vat for r in rows.values()), Decimal(0)) + (
        _money(rounding.group(1)) if rounding else 0
    )
    stated = {_money(m.group(1)) for m in re.finditer(rf"Celkem: ({_MONEY}) Kč", text)}
    if total not in stated:
        raise ParseFailed(
            f"Součet rekapitulace {total} nesedí s 'Celkem' na faktuře {sorted(stated)}"
        )
    return Expense(
        id=expense_id,
        document_number=number,
        supplier_name="Alza.cz a.s.",
        supplier_ico=ico,
        supplier_dic=dic,
        issue_date=issued,
        tax_point_date=_cz_date(duzp.group(1)) if duzp else issued,
        vat_rows=sorted(rows.values(), key=lambda r: r.rate, reverse=True),
        total=total,
        # Alza vystavuje fakturu až po zaplacení kartou / Apple Pay; převodem nebo na dobírku se úhrada potvrdí ručně.
        paid_date=issued
        if re.search(r"Způsob úhrady:[^\n]*(Pay|[Kk]art)", text)
        else None,
        source="imported",
    )


NEEDS_AI = "AI: přečti PDF a zapiš ho přes add_expense s pdf_path"


def parse_isdoc_expense(xml: bytes, expense_id: str, *, own_ico: str | None) -> Expense:
    """Přijatá faktura z ISDOC: dodavatel, rekapitulace DPH v Kč, celková částka."""
    doc = importer.load_isdoc(xml)
    ns = importer._NS

    def text(path: str) -> str | None:
        return importer._text(doc, path)

    def dec(el, path: str) -> Decimal:
        return Decimal(el.findtext(path, namespaces=ns) or "0")

    if text("i:DocumentType") != "1":
        raise ParseFailed(
            f"ISDOC typ dokladu {text('i:DocumentType')} (dobropis, záloha…) — {NEEDS_AI}"
        )
    supplier = "i:AccountingSupplierParty/i:Party/"
    country = text(supplier + "i:PostalAddress/i:Country/i:IdentificationCode") or "CZ"
    if country != "CZ":
        raise ParseFailed(
            f"Zahraniční dodavatel ({country}) — reverse charge se nehádá; {NEEDS_AI}"
        )
    customer_ico = text("i:AccountingCustomerParty/i:Party/i:PartyIdentification/i:ID")
    if own_ico and customer_ico and customer_ico != own_ico:
        raise ParseFailed(
            f"Faktura je vystavená na IČO {customer_ico}, ne na tvoje {own_ico}"
        )
    # Alza a jiní píšou „21.0000“ / „290.3900“ — v evidenci chceme sazbu „21“ a haléře.
    rows = [
        VatRow(
            rate=_plain_rate(dec(st, "i:TaxCategory/i:Percent")),
            base=_cents(dec(st, "i:TaxableAmount")),
            vat=_cents(dec(st, "i:TaxAmount")),
        )
        for st in doc.findall("i:TaxTotal/i:TaxSubTotal", ns)
    ]
    rows = [r for r in rows if r.base or r.vat]
    if not rows:
        raise ParseFailed(f"ISDOC nemá rekapitulaci DPH — {NEEDS_AI}")
    total_doc = doc.find("i:LegalMonetaryTotal", ns)
    inclusive = dec(total_doc, "i:TaxInclusiveAmount")
    recap = sum((r.base + r.vat for r in rows), Decimal(0))
    if abs(recap - inclusive) > Decimal("0.01"):
        raise ParseFailed(
            f"Rekapitulace DPH {recap} nesedí s celkem {inclusive} v ISDOC — {NEEDS_AI}"
        )
    issued = date.fromisoformat(text("i:IssueDate") or "")
    return Expense(
        id=expense_id,
        document_number=text("i:ID") or expense_id,
        supplier_name=text(supplier + "i:PartyName/i:Name") or "",
        supplier_ico=text(supplier + "i:PartyIdentification/i:ID"),
        supplier_dic=text(supplier + "i:PartyTaxScheme/i:CompanyID"),
        issue_date=issued,
        tax_point_date=date.fromisoformat(text("i:TaxPointDate") or issued.isoformat()),
        vat_rows=sorted(rows, key=lambda r: r.rate, reverse=True),
        total=_cents(inclusive + dec(total_doc, "i:PayableRoundingAmount")),
        source="imported",
    )


def _cents(d: Decimal) -> Decimal:
    return d.quantize(Decimal("0.01"))


def _plain_rate(d: Decimal) -> Decimal:
    return Decimal(int(d)) if d == d.to_integral_value() else d.normalize()


def _parse_document(doc_path: Path, expense_id: str, own_ico: str | None) -> Expense:
    """Pořadí: samostatný .isdoc (stejný název vedle PDF), ISDOC příloha v PDF, text faktury od Alzy; jinak ParseFailed."""
    sidecar = doc_path.with_suffix(".isdoc")
    if sidecar.exists():
        return parse_isdoc_expense(sidecar.read_bytes(), expense_id, own_ico=own_ico)
    xml = importer.find_isdoc(doc_path)
    if xml is not None:
        return parse_isdoc_expense(xml, expense_id, own_ico=own_ico)
    with pdfplumber.open(doc_path) as pdf:
        text = "\n".join(page.extract_text() or "" for page in pdf.pages)
    return parse_alza(text, expense_id)


def income_tax_cost(e: Expense, *, vat_payer: bool) -> Decimal:
    """Výdaj pro daň z příjmů. Plátce si odpočtené DPH do nákladů nedává; u reverse charge je `total` jen základ."""
    if not e.tax_deductible:
        return Decimal(0)
    claimed = e.vat_total if vat_payer and not e.reverse_charge else Decimal(0)
    return ((e.total - claimed) * e.business_share / 100).quantize(Decimal("0.01"))


def next_id(on: date) -> str:
    """ID nového výdaje = název PDF ve složce přijatých faktur, RRMMNNN podle měsíce DUZP."""
    key = on.strftime("%y%m")
    taken = [p.stem for p in expenses_dir().glob(f"{key}*.json")]
    folder = received_dir()
    if folder.exists():
        taken += [p.stem for p in folder.rglob(f"{key}*.pdf")]
    seqs = [int(t[4:7]) for t in taken if re.fullmatch(rf"{key}\d{{3}}", t)]
    return f"{key}{max(seqs, default=0) + 1:03d}"


def received_path(expense_id: str, on: date) -> Path:
    return received_dir() / on.strftime("%y_%m_prijate") / f"{expense_id}.pdf"


def mark_paid(expense_id: str, paid_on: date) -> Expense:
    expense = Expense.model_validate_json(_path(expense_id).read_text(encoding="utf-8"))
    if expense.paid_date:
        raise ValueError(f"Výdaj {expense_id} už je zaplacený {expense.paid_date}")
    expense = expense.model_copy(update={"paid_date": paid_on})
    _path(expense_id).write_text(
        expense.model_dump_json(indent=2) + "\n", encoding="utf-8"
    )
    return expense


def _path(expense_id: str) -> Path:
    return expenses_dir() / f"{expense_id}.json"


def save(expense: Expense) -> Expense:
    if _path(expense.id).exists():
        raise ValueError(f"Výdaj {expense.id} už v evidenci je")
    expenses_dir().mkdir(parents=True, exist_ok=True)
    _path(expense.id).write_text(
        expense.model_dump_json(indent=2) + "\n", encoding="utf-8"
    )
    return expense


def remove(expense_id: str) -> Expense:
    expense = Expense.model_validate_json(_path(expense_id).read_text(encoding="utf-8"))
    _path(expense_id).unlink()
    return expense


def list_expenses(year: int | None = None) -> list[Expense]:
    items = [
        Expense.model_validate_json(p.read_text(encoding="utf-8"))
        for p in expenses_dir().glob("*.json")
    ]
    if year:
        items = [e for e in items if e.tax_point_date.year == year]
    return sorted(items, key=lambda e: (e.tax_point_date, e.id))


def issues_file() -> Path:
    return home_dir() / "expense_issues.json"


def load_issues() -> list[ExpenseIssue]:
    path = issues_file()
    if not path.exists():
        return []
    return [
        ExpenseIssue.model_validate(i)
        for i in json.loads(path.read_text(encoding="utf-8"))
    ]


def _year_of(pdf_path: Path) -> int | None:
    m = re.search(r"(?<!\d)(\d{2})\d{4,5}(?!\d)", pdf_path.stem)
    return 2000 + int(m.group(1)) if m else None


def _documents(folder: Path) -> list[Path]:
    """PDF přijatých faktur + samostatné .isdoc, ke kterým PDF chybí (.isdoc vedle PDF čte _parse_document)."""
    if not folder.exists():
        return []
    pdfs = sorted(folder.rglob("*.pdf"))
    alone = [
        p for p in sorted(folder.rglob("*.isdoc")) if not p.with_suffix(".pdf").exists()
    ]
    return pdfs + alone


def sync(folder: Path | None = None, *, since_year: int | None = None) -> list[str]:
    """Načte nová PDF přijatých faktur; co neumí, zapíše do expense_issues.json."""
    folder = folder or received_dir()
    since_year = since_year or date.today().year - 1
    known = {e.pdf_path.resolve() for e in list_expenses() if e.pdf_path}
    previous = {i.pdf_path: i for i in load_issues()}
    own_ico = load_supplier().ico if supplier_file().exists() else None
    imported: list[str] = []
    issues: list[ExpenseIssue] = []
    for pdf_path in _documents(folder):
        year = _year_of(pdf_path)
        if pdf_path.resolve() in known or year is None or year < since_year:
            continue
        expense_id = pdf_path.stem
        try:
            expense = _parse_document(pdf_path, expense_id, own_ico).model_copy(
                update={"pdf_path": pdf_path}
            )
            save(expense)
            imported.append(expense_id)
        except (ParseFailed, importer.ImportFailed, ValueError) as exc:
            old = previous.get(pdf_path)
            issues.append(
                ExpenseIssue(
                    pdf_path=pdf_path,
                    reason=str(exc),
                    detected_at=old.detected_at if old else datetime.now(),
                )
            )
    for expense in list_expenses():
        if expense.pdf_path and not expense.pdf_path.exists():
            moved = (
                next(folder.rglob(f"{expense.id}{expense.pdf_path.suffix}"), None)
                if folder.exists()
                else None
            )
            if moved:
                _path(expense.id).write_text(
                    expense.model_copy(update={"pdf_path": moved}).model_dump_json(
                        indent=2
                    )
                    + "\n",
                    encoding="utf-8",
                )
                continue
            old = previous.get(expense.pdf_path)
            issues.append(
                ExpenseIssue(
                    pdf_path=expense.pdf_path,
                    reason=f"PDF výdaje {expense.id} chybí — pokud byl doklad smazán záměrně, odeber ho z evidence",
                    detected_at=old.detected_at if old else datetime.now(),
                )
            )
    issues_file().parent.mkdir(parents=True, exist_ok=True)
    issues_file().write_text(
        json.dumps(
            [i.model_dump(mode="json") for i in issues], indent=2, ensure_ascii=False
        )
        + "\n",
        encoding="utf-8",
    )
    return imported
