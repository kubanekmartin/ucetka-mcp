import json
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
import yaml

from ucetka import ledger
from ucetka.config import load_supplier
from ucetka.issue import issue
from ucetka.models import Invoice

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"


def _invoice(number: str, **changes) -> Invoice:
    data = yaml.safe_load((EXAMPLES / "2605001.yaml").read_text())
    data["supplier"] = load_supplier(EXAMPLES / "supplier.yaml").party().model_dump()
    data["number"] = number
    data.update(changes)
    return Invoice.model_validate(data)


def test_numbering_per_issue_month() -> None:
    assert ledger.next_number(date(2026, 5, 3)) == "2605001"
    issue(_invoice("2605001"))
    issue(_invoice("2605002"))
    assert ledger.next_number(date(2026, 5, 20)) == "2605003"
    assert ledger.next_number(date(2026, 6, 1)) == "2606001"


def test_legacy_state_counter_is_floor(ucetka_home: Path) -> None:
    (ucetka_home / "state.json").write_text(json.dumps({"counters": {"2605": 7}}))
    assert ledger.next_number(date(2026, 5, 3)) == "2605008"


def test_failed_issue_does_not_consume_number(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*args, **kwargs):
        raise RuntimeError("render failed")

    monkeypatch.setattr("ucetka.issue.render_invoice_pdf", boom)
    with pytest.raises(RuntimeError):
        issue(_invoice("2605001"))
    assert ledger.next_number(date(2026, 5, 3)) == "2605001"
    assert ledger.list_records() == []


def test_duplicate_number_rejected() -> None:
    issue(_invoice("2605001"))
    with pytest.raises(ValueError, match="už v evidenci"):
        issue(_invoice("2605001"))


def test_record_roundtrip_and_mark_paid() -> None:
    record = issue(_invoice("2605001"))
    assert record.pdf_path.exists()
    loaded = ledger.get("2605001")
    assert loaded.invoice == record.invoice
    assert loaded.status == "issued"
    paid = ledger.mark_paid("2605001", date(2026, 5, 20))
    assert paid.status == "paid"
    assert ledger.list_records(status="paid")[0].paid_date == date(2026, 5, 20)
    assert ledger.list_records(status="issued") == []


def test_overdue_flag() -> None:
    past = date.today() - timedelta(days=30)
    record = issue(_invoice("2001001", issue_date=past, tax_point_date=past, due_date=past + timedelta(days=14)))
    assert record.overdue


def test_list_filters_by_month() -> None:
    issue(_invoice("2605001"))
    june = date(2026, 6, 2)
    issue(_invoice("2606001", issue_date=june, tax_point_date=june, due_date=june))
    assert [r.invoice.number for r in ledger.list_records(month="2026-06")] == ["2606001"]


def test_missing_invoice_raises() -> None:
    with pytest.raises(ledger.InvoiceNotFound):
        ledger.get("9999999")


def test_numbering_respects_pdfs_outside_ledger(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    folder = tmp_path / "vydane" / "26_10_vydane"
    folder.mkdir(parents=True)
    (folder / "faktura_2610004.pdf").write_bytes(b"%PDF")
    monkeypatch.setenv("UCETKA_OUT", str(tmp_path / "vydane"))
    assert ledger.next_number(date(2026, 10, 1)) == "2610005"


def test_cancelled_invoice_excluded_from_overview() -> None:
    from ucetka import overview

    issue(_invoice("2605001"))
    issue(_invoice("2605002"))
    ledger.cancel("2605002", "chybná")
    o = overview.build(2026, date(2026, 10, 4), sync=False)
    assert o.issued_net == Decimal("5000.00")
    assert [r.invoice.number for r in o.unpaid] == ["2605001"]
    assert ledger.next_number(date(2026, 5, 20)) == "2605003"


def test_pdf_goes_to_folder_of_tax_point_month() -> None:
    inv = _invoice("2609009", issue_date=date(2026, 10, 8), tax_point_date=date(2026, 9, 30), due_date=date(2026, 11, 7))
    assert issue(inv).pdf_path.parent.name == "26_09_vydane"
