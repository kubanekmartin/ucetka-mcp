from datetime import date, timedelta
from decimal import Decimal
from io import BytesIO
from pathlib import Path

import pikepdf
import pytest
import yaml
from fastapi.testclient import TestClient

from ucetka import ledger
from ucetka.config import load_supplier
from ucetka.models import Invoice
from ucetka.web.app import app

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"


@pytest.fixture(scope="module")
def client() -> TestClient:
    return TestClient(app)


def test_index_returns_html_form(client: TestClient) -> None:
    r = client.get("/")
    assert r.status_code == 200
    assert "ucetka" in r.text
    assert "<form" in r.text


def test_issue_inline_customer_returns_pdf(client: TestClient, tmp_path: Path) -> None:
    r = client.post(
        "/issue",
        data={
            "customer_name": "Test Customer s.r.o.",
            "customer_ico": "12345678",
            "customer_dic": "CZ12345678",
            "customer_street": "Hlavní 1",
            "customer_city": "Brno",
            "customer_zip": "60200",
            "number": "2605999",
            "issue_date": "2026-05-17",
            "due_date": "2026-05-31",
            "item_description": "Test služba",
            "item_quantity": "1",
            "item_unit": "ks",
            "item_unit_price_net": "1000",
            "item_vat_rate": "21",
            "bank_account": "2000145399/0100",
            "variable_symbol": "2605999",
        },
    )
    assert r.status_code == 200, r.text
    assert r.headers["content-type"] == "application/pdf"
    assert "faktura_2605999.pdf" in r.headers["content-disposition"]
    pdf = pikepdf.Pdf.open(BytesIO(r.content))
    assert "invoice.isdoc" in pdf.attachments


def test_issue_via_customer_id(client: TestClient) -> None:
    r = client.post(
        "/issue",
        data={
            "customer_id": "vzorova-firma",
            "number": "2605998",
            "issue_date": "2026-05-17",
            "due_date": "2026-05-31",
            "item_description": "Konzultace",
            "item_unit_price_net": "5000",
            "item_vat_rate": "21",
            "bank_account": "2000145399/0100",
        },
    )
    assert r.status_code == 200, r.text
    assert r.headers["content-type"] == "application/pdf"


def test_issue_missing_required_returns_error(client: TestClient) -> None:
    r = client.post("/issue", data={})
    assert r.status_code == 422  # FastAPI/Pydantic validation


def _save(
    number: str, customer: str, issued: date, due: date, net: str = "1000"
) -> None:
    data = yaml.safe_load((EXAMPLES / "2605001.yaml").read_text())
    data["supplier"] = load_supplier(EXAMPLES / "supplier.yaml").party().model_dump()
    data["customer"]["name"] = customer
    data |= {
        "number": number,
        "issue_date": issued,
        "tax_point_date": issued,
        "due_date": due,
    }
    data["lines"][0]["unit_price_net"] = net
    ledger.save(Invoice.model_validate(data), Path(f"{number}.pdf"))


@pytest.fixture
def ledger_with_states() -> date:
    today = date.today()
    _save(
        "9001001",
        "Po Splatnosti s.r.o.",
        today - timedelta(days=30),
        today - timedelta(days=12),
        "2000",
    )
    _save(
        "9001002",
        "Brzy Splatná a.s.",
        today - timedelta(days=5),
        today + timedelta(days=3),
    )
    _save("9001003", "Daleko Splatná z.s.", today, today + timedelta(days=30))
    _save(
        "9001004",
        "Zaplacená s.r.o.",
        today - timedelta(days=40),
        today - timedelta(days=20),
    )
    ledger.mark_paid("9001004", today - timedelta(days=21))
    _save(
        "9001005",
        "Stornovaná s.r.o.",
        today - timedelta(days=40),
        today - timedelta(days=20),
    )
    ledger.cancel("9001005", "test")
    return today


def test_faktury_lists_all_invoices_with_status(
    client: TestClient, ledger_with_states: date
) -> None:
    r = client.get("/faktury")
    assert r.status_code == 200
    for number in ("9001001", "9001002", "9001003", "9001004", "9001005"):
        assert f'href="/faktura/{number}.pdf"' in r.text
    table = r.text[r.text.index("<tbody>") :]
    assert table.index("9001003") < table.index("9001001"), "nejnovější nahoře"
    assert "Po splatnosti 12 dní" in r.text
    assert "Zaplaceno" in r.text and "Stornováno" in r.text and "Nezaplaceno" in r.text
    assert "2\xa0420,00\xa0Kč" in r.text


def test_overdue_and_due_soon_alert(
    client: TestClient, ledger_with_states: date
) -> None:
    for url in ("/faktury", "/prehled"):
        r = client.get(url)
        assert r.status_code == 200, url
        assert "1 faktura je po splatnosti" in r.text
        assert "12 dní po splatnosti" in r.text
        assert "Splatné v příštích 7 dnech" in r.text
        assert "za 3 dny" in r.text
        assert '<span class="count" title="Po splatnosti">1<' in r.text


def test_no_alert_when_nothing_due(client: TestClient) -> None:
    r = client.get("/faktury")
    assert r.status_code == 200
    assert "Upozornění na splatnost" not in r.text
    assert 'class="count"' not in r.text


def test_mark_paid_from_list(client: TestClient, ledger_with_states: date) -> None:
    r = client.post(
        "/faktury/zaplaceno",
        data={"number": "9001001", "paid_on": "2026-10-01"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/faktury?zaplaceno=9001001"
    record = ledger.get("9001001")
    assert record.status == "paid"
    assert record.paid_date == date(2026, 10, 1)
    page = client.get(r.headers["location"]).text
    assert "Faktura 9001001 je označená jako zaplacená." in page
    assert 'class="count"' not in page


def test_mark_paid_unknown_invoice_404(client: TestClient) -> None:
    r = client.post(
        "/faktury/zaplaceno", data={"number": "0000000", "paid_on": "2026-10-01"}
    )
    assert r.status_code == 404


def test_issue_with_multiple_lines_and_custom_unit(client: TestClient) -> None:
    r = client.post(
        "/issue",
        data={
            "customer_name": "Test Customer s.r.o.",
            "customer_dic": "CZ12345678",
            "customer_street": "Hlavní 1",
            "customer_city": "Brno",
            "customer_zip": "60200",
            "number": "2605998",
            "issue_date": "2026-05-17",
            "due_date": "2026-05-31",
            "item_description": ["Vývoj", "Licence", "Cestovné"],
            "item_quantity": ["10", "1", "120"],
            "item_unit": ["h", "licence", "km"],
            "item_unit_price_net": ["1 250,50", "3000", "5,20"],
            "item_vat_rate": ["21", "21", "21"],
        },
    )
    assert r.status_code == 200, r.text
    inv = ledger.get("2605998").invoice
    assert [(ln.unit, ln.quantity) for ln in inv.lines] == [("h", 10), ("licence", 1), ("km", 120)]
    assert inv.total_net == Decimal("16129.00")


def test_index_prefills_supplier_account(client: TestClient) -> None:
    html = client.get("/").text
    assert 'value="19-2000145399/0100"' in html
    assert 'list="units"' in html and "+ Přidat položku" in html


def test_expenses_page_mark_paid_and_book(client: TestClient) -> None:
    from ucetka import expenses

    expenses.save(expenses.Expense(
        id="2603001", document_number="A1", supplier_name="Alza.cz a.s.", supplier_dic="CZ27082440",
        issue_date=date(2026, 3, 1), tax_point_date=date(2026, 3, 1),
        vat_rows=[expenses.VatRow(rate=Decimal(21), base=Decimal(1000), vat=Decimal(210))], total=Decimal(1210),
    ))
    assert "A1" in client.get("/vydaje?year=2026").text
    r = client.post("/vydaje/zaplaceno", data={"expense_id": "2603001", "paid_on": "2026-03-05", "year": "2026"},
                    follow_redirects=False)
    assert r.status_code == 303
    assert str(expenses.list_expenses()[0].paid_date) == "2026-03-05"
    r = client.post("/vydaje/kniha", data={"year": "2026"})
    assert r.status_code == 200 and "Kniha vytvořena" in r.text
