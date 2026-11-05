"""Automatický import vydaných faktur (PDF s ISDOC přílohou) do evidence.

Co nejde načíst nebo nesedí, skončí v `import_issues.json` jako flag k ručnímu zadání.
"""

from __future__ import annotations

import json
import re
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import pikepdf
from lxml import etree
from pydantic import BaseModel, ValidationError

from ucetka import ledger
from ucetka.config import home_dir, output_dir
from ucetka.models import Address, Invoice, Party

_NAMESPACES = ("http://isdoc.cz/namespace/2013", "http://isdoc.cz/namespace/invoice")
_NS = {"i": _NAMESPACES[0]}
# FakturaOnline zaokrouhluje částku k úhradě na koruny, základ a DPH sedí na haléř.
_PAYABLE_TOLERANCE = Decimal("1.00")


class ImportFailed(Exception):
    pass


class ImportIssue(BaseModel):
    pdf_path: Path
    reason: str
    detected_at: datetime


class SyncResult(BaseModel):
    imported: list[str]
    issues: list[ImportIssue]


def issues_file() -> Path:
    return home_dir() / "import_issues.json"


def _text(el: etree._Element, path: str) -> str | None:
    value = el.findtext(path, namespaces=_NS)
    return value.strip() if value and value.strip() else None


def _dec(el: etree._Element, path: str) -> Decimal:
    value = _text(el, path)
    if value is None:
        raise ImportFailed(f"ISDOC nemá {path}")
    return Decimal(value)


def _party(el: etree._Element, vat_payer: bool) -> Party:
    p = el.find("i:Party", _NS)
    street = _text(p, "i:PostalAddress/i:StreetName") or ""
    building = _text(p, "i:PostalAddress/i:BuildingNumber")
    return Party(
        name=_text(p, "i:PartyName/i:Name") or "",
        ico=_text(p, "i:PartyIdentification/i:ID"),
        dic=_text(p, "i:PartyTaxScheme/i:CompanyID"),
        vat_payer=vat_payer,
        address=Address(
            street=f"{street} {building}" if building else street,
            city=_text(p, "i:PostalAddress/i:CityName") or "",
            zip=_text(p, "i:PostalAddress/i:PostalZone") or "",
            country=_text(p, "i:PostalAddress/i:Country/i:Name") or "Česká republika",
            country_code=_text(p, "i:PostalAddress/i:Country/i:IdentificationCode")
            or "CZ",
        ),
    )


def load_isdoc(xml: bytes) -> etree._Element:
    """Kořen ISDOC v namespace 6.x (5.x se přejmenuje); nevalidní XML → ImportFailed."""
    try:
        doc = etree.fromstring(xml)
    except etree.XMLSyntaxError as exc:
        raise ImportFailed(f"ISDOC není validní XML: {exc}") from exc
    if etree.QName(doc).namespace not in _NAMESPACES:
        raise ImportFailed(f"Neznámý ISDOC namespace {etree.QName(doc).namespace}")
    if etree.QName(doc).namespace != _NS["i"]:
        # ISDOC 5.x má jiný namespace, ale stejnou strukturu prvků, které čteme.
        for el in doc.iter():
            if isinstance(el.tag, str):
                el.tag = f"{{{_NS['i']}}}{etree.QName(el).localname}"
    return doc


def parse_isdoc(xml: bytes) -> Invoice:
    doc = load_isdoc(xml)
    if _text(doc, "i:DocumentType") != "1":
        raise ImportFailed(
            f"Typ dokladu {_text(doc, 'i:DocumentType')} (jen faktura 1 se importuje automaticky)"
        )
    vat_payer = _text(doc, "i:VATApplicable") == "true"
    foreign = _text(doc, "i:ForeignCurrencyCode")
    rate = _dec(doc, "i:CurrRate") if foreign else None
    rate_amount = int(_dec(doc, "i:RefCurrRate")) if foreign else 1

    lines = []
    for line in doc.findall("i:InvoiceLines/i:InvoiceLine", _NS):
        qty = _dec(line, "i:InvoicedQuantity")
        net = _dec(
            line, "i:LineExtensionAmountCurr" if foreign else "i:LineExtensionAmount"
        )
        lines.append(
            {
                "quantity": qty,
                "unit": line.find("i:InvoicedQuantity", _NS).get("unitCode") or "ks",
                "description": _text(line, "i:Item/i:Description") or "(bez popisu)",
                "unit_price_net": net / qty if qty else net,
                "vat_rate": _dec(line, "i:ClassifiedTaxCategory/i:Percent").normalize(),
            }
        )

    issue_date = date.fromisoformat(_text(doc, "i:IssueDate"))
    details = doc.find("i:PaymentMeans/i:Payment/i:Details", _NS)
    account_id = _text(details, "i:ID") if details is not None else None
    bank_account = None
    if account_id:
        prefix, number = (
            account_id.split("-", 1) if "-" in account_id else (None, account_id)
        )
        bank_account = {
            "prefix": prefix,
            "number": number,
            "bank_code": _text(details, "i:BankCode"),
        }
    due = _text(details, "i:PaymentDueDate") if details is not None else None
    vs = _text(details, "i:VariableSymbol") if details is not None else None
    if not _text(doc, "i:AccountingCustomerParty/i:Party/i:PartyName/i:Name"):
        raise ImportFailed("Chybí odběratel (§ 29 ZDPH) — doplň ho ručně")
    customer = _party(
        doc.find("i:AccountingCustomerParty", _NS),
        vat_payer=bool(
            _text(doc, "i:AccountingCustomerParty/i:Party/i:PartyTaxScheme/i:CompanyID")
        ),
    )
    try:
        inv = Invoice(
            number=_text(doc, "i:ID"),
            issue_date=issue_date,
            tax_point_date=date.fromisoformat(
                _text(doc, "i:TaxPointDate") or issue_date.isoformat()
            ),
            due_date=date.fromisoformat(due) if due else issue_date,
            supplier=_party(
                doc.find("i:AccountingSupplierParty", _NS), vat_payer=vat_payer
            ),
            customer=customer,
            payment={
                "bank_account": bank_account,
                "variable_symbol": vs or _text(doc, "i:ID"),
            },
            lines=lines,
            currency=foreign or "CZK",
            exchange_rate=rate,
            exchange_rate_amount=rate_amount,
            reverse_charge=vat_payer
            and bool(customer.dic)
            and not customer.dic.upper().startswith("CZ")
            and all(ln["vat_rate"] == 0 for ln in lines),
        )
    except ValidationError as exc:
        raise ImportFailed(f"Data neprošla validací: {exc.errors()[0]['msg']}") from exc

    _check_totals(inv, doc, foreign is not None)
    note = _text(doc, "i:Note")
    if note:
        # Náš generátor skládá do Note i zákonné texty — ty se odvodí znovu z dat faktury.
        for legal in inv.legal_notes:
            note = note.replace(legal, "")
        inv = inv.model_copy(update={"note": note.strip() or None})
    return inv


def _check_totals(inv: Invoice, doc: etree._Element, foreign: bool) -> None:
    suffix = "Curr" if foreign else ""
    expected_net = _dec(doc, f"i:LegalMonetaryTotal/i:TaxExclusiveAmount{suffix}")
    expected_vat = _dec(doc, f"i:TaxTotal/i:TaxAmount{suffix}")
    expected_payable = _dec(doc, f"i:LegalMonetaryTotal/i:PayableAmount{suffix}")
    if inv.total_net != expected_net:
        raise ImportFailed(
            f"Základ nesedí: přepočet {inv.total_net}, ISDOC {expected_net}"
        )
    if abs(inv.total_vat - expected_vat) > Decimal("0.01"):
        raise ImportFailed(
            f"DPH nesedí: přepočet {inv.total_vat}, ISDOC {expected_vat}"
        )
    if abs(inv.total_gross - expected_payable) > _PAYABLE_TOLERANCE:
        raise ImportFailed(
            f"Částka k úhradě nesedí: přepočet {inv.total_gross}, ISDOC {expected_payable}"
        )


def find_isdoc(path: Path) -> bytes | None:
    """Obsah ISDOC přílohy PDF, nebo None, když PDF žádnou nemá."""
    with pikepdf.open(path) as pdf:
        # Pohoda ukládá název přílohy s escapovanou tečkou („…invoice2Eisdoc“), proto ne endswith(".isdoc").
        names = [n for n in pdf.attachments if n.lower().endswith("isdoc")]
        return pdf.attachments[names[0]].get_file().read_bytes() if names else None


def read_pdf(path: Path) -> Invoice:
    xml = find_isdoc(path)
    if xml is None:
        raise ImportFailed("PDF nemá ISDOC přílohu")
    return parse_isdoc(xml)


def _known_pdfs() -> set[Path]:
    return {r.pdf_path.resolve() for r in ledger.list_records()}


def load_issues() -> list[ImportIssue]:
    path = issues_file()
    if not path.exists():
        return []
    return [
        ImportIssue.model_validate(i)
        for i in json.loads(path.read_text(encoding="utf-8"))
    ]


def _year_of(pdf_path: Path) -> int | None:
    """Rok z názvu faktury_YYMMNNN.pdf — starší roky (už podané) se automaticky neimportují."""
    m = re.search(r"(\d{2})\d{4,5}", pdf_path.stem)
    return 2000 + int(m.group(1)) if m else None


def sync(folder: Path | None = None, *, since_year: int | None = None) -> SyncResult:
    """Načte nová PDF z adresáře vydaných faktur (default letos a loni). Flagy se přepočítají při každém běhu."""
    folder = folder or output_dir()
    since_year = since_year or date.today().year - 1
    known = _known_pdfs()
    previous = {i.pdf_path: i for i in load_issues()}
    imported: list[str] = []
    issues: list[ImportIssue] = []
    for pdf_path in sorted(folder.rglob("*.pdf")):
        year = _year_of(pdf_path)
        if pdf_path.resolve() in known or (year is not None and year < since_year):
            continue
        try:
            inv = read_pdf(pdf_path)
            if ledger.exists(inv.number):
                if ledger.get(inv.number).pdf_path.exists():
                    raise ImportFailed(
                        f"Číslo {inv.number} už v evidenci je u jiného PDF"
                    )
                ledger.relink_pdf(inv.number, pdf_path)
                continue
            ledger.save(inv, pdf_path, source="imported")
            imported.append(inv.number)
        except (ImportFailed, pikepdf.PdfError) as exc:
            old = previous.get(pdf_path)
            issues.append(
                ImportIssue(
                    pdf_path=pdf_path,
                    reason=str(exc),
                    detected_at=old.detected_at if old else datetime.now(),
                )
            )
    # Smazané PDF záznam neruší — vydaná faktura se z evidence nemaže, jen se na to upozorní.
    for record in ledger.list_records():
        if record.status != "cancelled" and not record.pdf_path.exists():
            old = previous.get(record.pdf_path)
            issues.append(
                ImportIssue(
                    pdf_path=record.pdf_path,
                    reason=f"PDF faktury {record.invoice.number} chybí (smazané nebo přesunuté mimo složku)",
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
    return SyncResult(imported=imported, issues=issues)
