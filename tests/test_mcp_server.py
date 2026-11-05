from decimal import Decimal

import pytest
from pydantic import ValidationError

from ucetka import ledger, mcp_server
from ucetka.mcp_server import (
    LineInput,
    draft_invoice_email,
    export_invoice_pohoda,
    get_invoice,
    get_supplier,
    issue_invoice,
    list_customers,
    list_invoices,
    mark_paid,
    peek_invoice_number,
)


def test_list_customers_includes_vzorova_firma() -> None:
    assert "vzorova-firma" in list_customers()


def test_get_supplier_shows_default_account() -> None:
    out = get_supplier()
    assert "Petr Svoboda" in out
    assert "19-2000145399/0100" in out


def test_issue_with_default_account_and_multiple_lines() -> None:
    out = issue_invoice(
        lines=[
            LineInput(description="Konzultace", unit_price=Decimal(1500), quantity=Decimal(10), unit="h"),
            LineInput(description="Kniha", unit_price=Decimal(400), vat_rate=Decimal(12)),
        ],
        customer_id="vzorova-firma",
        issue_date="2026-10-04",
    )
    assert "Vystaveno" in out
    record = ledger.get("2610001")
    assert record.invoice.total_net == Decimal("15400.00")
    assert record.invoice.total_vat == Decimal("3198.00")
    assert record.pdf_path.exists()
    assert peek_invoice_number("2026-10-20") == "2610002"


def test_issue_validation_error_does_not_consume_number() -> None:
    with pytest.raises(ValidationError):
        issue_invoice(
            lines=[LineInput(description="x", unit_price=Decimal(100), vat_rate=Decimal(21))],
            customer_id="vzorova-firma",
            issue_date="2026-10-04",
            tax_point_date="2026-10-04",
            due_days=14,
            reverse_charge=True,
        )
    assert peek_invoice_number("2026-10-04") == "2610001"


def test_issue_requires_customer() -> None:
    with pytest.raises(ValueError, match="customer_id"):
        issue_invoice(lines=[LineInput(description="x", unit_price=Decimal(1))])


def test_issue_eur_fetches_cnb_rate(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mcp_server, "fetch_rate", lambda cur, on: (Decimal("24.465"), 1))
    issue_invoice(
        lines=[LineInput(description="AI workshop", unit_price=Decimal(100), quantity=Decimal(4), unit="h")],
        customer_name="Beispiel GmbH",
        customer_dic="DE123456789",
        customer_street="Musterstraße 1",
        customer_city="Berlin",
        customer_zip="10115",
        customer_country="Německo",
        customer_country_code="DE",
        currency="EUR",
        reverse_charge=True,
        issue_date="2026-10-04",
    )
    inv = ledger.get("2610001").invoice
    assert inv.total_gross == Decimal("400.00")
    assert inv.total_net_czk == Decimal("9786.00")


def test_issue_by_ico_uses_ares(monkeypatch: pytest.MonkeyPatch) -> None:
    from ucetka import ares
    from ucetka.customers import get_customer

    monkeypatch.setattr(mcp_server.ares, "lookup", lambda ico: get_customer("vzorova-firma"))
    issue_invoice(lines=[LineInput(description="x", unit_price=Decimal(100))], customer_ico="87654326",
                  issue_date="2026-10-04")
    assert ledger.get("2610001").invoice.customer.ico == "87654326"
    assert ares.lookup is not None


def test_list_get_paid_export_flow() -> None:
    issue_invoice(lines=[LineInput(description="Přednáška", unit_price=Decimal(5000))], customer_id="vzorova-firma",
                  issue_date="2026-10-04")
    assert "2610001" in list_invoices(status="issued")
    detail = get_invoice("2610001")
    assert "SPAYD: SPD*1.0*" in detail
    assert "zaplacena" in mark_paid(["2610001"], "2026-10-10")
    assert list_invoices(status="issued") == "Žádné faktury."
    out = export_invoice_pohoda("2610001")
    assert "faktura_2610001.pohoda.xml" in out


def test_draft_email_uses_customer_email(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []
    monkeypatch.setattr(mcp_server.mail, "create_draft", lambda *args: calls.append(args))
    issue_invoice(lines=[LineInput(description="Přednáška", unit_price=Decimal(5000))], customer_id="vzorova-firma",
                  issue_date="2026-10-04")
    out = draft_invoice_email("2610001")
    to, subject, body, attachment = calls[0]
    assert to == "faktury@example.com"
    assert "2610001" in subject
    assert "6\xa0050,00 Kč" in body
    assert attachment.name == "faktura_2610001.pdf"
    assert "odešli ručně" in out
