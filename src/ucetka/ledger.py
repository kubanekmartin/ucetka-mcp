"""Evidence vydaných faktur: jeden JSON na fakturu v ~/.ucetka/invoices/.

Ledger je jediný zdroj číslování — číslo vznikne až uložením úspěšně vystavené faktury.
"""
from __future__ import annotations

import json
import re
from datetime import date, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict

from ucetka.config import invoices_dir, output_dir, state_file
from ucetka.models import Invoice

_NUMBER_RE = re.compile(r"^(\d{4})(\d{3,})$")
_PDF_NUMBER_RE = re.compile(r"(\d{4})(\d{3,})")

Status = Literal["issued", "paid", "cancelled"]
Source = Literal["issued", "imported", "manual"]


class InvoiceNotFound(KeyError):
    pass


class InvoiceRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    invoice: Invoice
    status: Status = "issued"
    paid_date: date | None = None
    pdf_path: Path
    issued_at: datetime
    source: Source = "issued"
    cancel_reason: str | None = None

    @property
    def overdue(self) -> bool:
        return self.status == "issued" and self.invoice.due_date < date.today()


def _path(number: str) -> Path:
    return invoices_dir() / f"{number}.json"


def _legacy_counter(month_key: str) -> int:
    path = state_file()
    if not path.exists():
        return 0
    return json.loads(path.read_text(encoding="utf-8")).get("counters", {}).get(month_key, 0)


def next_number(on: date) -> str:
    """YYMM + pořadí v měsíci (např. 2610001).

    Bere v úvahu i PDF ve složce vydaných faktur, která v evidenci nejsou (flagované importy).
    """
    key = on.strftime("%y%m")
    seqs = [_legacy_counter(key)]
    for p in invoices_dir().glob("*.json"):
        m = _NUMBER_RE.match(p.stem)
        if m and m.group(1) == key:
            seqs.append(int(m.group(2)))
    out = output_dir()
    if out.exists():
        for p in out.rglob(f"*{key}*.pdf"):
            m = _PDF_NUMBER_RE.search(p.stem)
            if m and m.group(1) == key:
                seqs.append(int(m.group(2)))
    return f"{key}{max(seqs) + 1:03d}"


def exists(number: str) -> bool:
    return _path(number).exists()


def _write(record: InvoiceRecord) -> None:
    invoices_dir().mkdir(parents=True, exist_ok=True)
    _path(record.invoice.number).write_text(record.model_dump_json(indent=2) + "\n", encoding="utf-8")


def save(invoice: Invoice, pdf_path: Path, *, source: Source = "issued") -> InvoiceRecord:
    if exists(invoice.number):
        raise ValueError(f"Faktura {invoice.number} už v evidenci je")
    record = InvoiceRecord(invoice=invoice, pdf_path=pdf_path, issued_at=datetime.now(), source=source)
    _write(record)
    return record


def get(number: str) -> InvoiceRecord:
    path = _path(number)
    if not path.exists():
        raise InvoiceNotFound(f"Faktura {number} v evidenci není")
    return InvoiceRecord.model_validate_json(path.read_text(encoding="utf-8"))


def list_records(*, status: Status | None = None, month: str | None = None) -> list[InvoiceRecord]:
    """`month` ve tvaru YYYY-MM filtruje podle data vystavení."""
    records = [InvoiceRecord.model_validate_json(p.read_text(encoding="utf-8")) for p in invoices_dir().glob("*.json")]
    if status:
        records = [r for r in records if r.status == status]
    if month:
        records = [r for r in records if r.invoice.issue_date.strftime("%Y-%m") == month]
    return sorted(records, key=lambda r: (r.invoice.issue_date, r.invoice.number))


def relink_pdf(number: str, pdf_path: Path) -> InvoiceRecord:
    record = get(number).model_copy(update={"pdf_path": pdf_path})
    _write(record)
    return record


def cancel(number: str, reason: str) -> InvoiceRecord:
    """Storno: číslo zůstává v řadě kvůli dohledatelnosti, do příjmů ani DPH se nepočítá."""
    record = get(number).model_copy(update={"status": "cancelled", "cancel_reason": reason})
    _write(record)
    return record


def mark_paid(number: str, paid_date: date) -> InvoiceRecord:
    record = get(number).model_copy(update={"status": "paid", "paid_date": paid_date})
    _write(record)
    return record
