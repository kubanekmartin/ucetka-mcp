from decimal import Decimal
from pathlib import Path

import yaml

from ucetka.config import load_supplier
from ucetka.models import Invoice

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"


def _load_invoice() -> Invoice:
    supplier = load_supplier(EXAMPLES / "supplier.yaml").party()
    data = yaml.safe_load((EXAMPLES / "2605001.yaml").read_text())
    data["supplier"] = supplier.model_dump()
    return Invoice.model_validate(data)


def test_invoice_loads_from_yaml() -> None:
    inv = _load_invoice()
    assert inv.number == "2605001"
    assert inv.supplier.name == "Petr Svoboda"
    assert inv.customer.name == "Vzorová firma s.r.o."
    assert inv.supplier.ico == "98765434"
    assert inv.customer.dic == "CZ87654326"


def test_totals_match_reference() -> None:
    inv = _load_invoice()
    assert inv.total_net == Decimal("5000.00")
    assert inv.total_vat == Decimal("1050.00")
    assert inv.total_gross == Decimal("6050.00")


def test_line_calculations() -> None:
    inv = _load_invoice()
    ln = inv.lines[0]
    assert ln.net_total == Decimal("5000.00")
    assert ln.vat_total == Decimal("1050.00")
    assert ln.gross_total == Decimal("6050.00")


def test_vat_breakdown() -> None:
    inv = _load_invoice()
    rows = inv.vat_breakdown()
    assert len(rows) == 1
    row = rows[0]
    assert row.rate == Decimal("21")
    assert row.base == Decimal("5000.00")
    assert row.vat == Decimal("1050.00")
    assert row.total == Decimal("6050.00")


def test_bank_account_string() -> None:
    inv = _load_invoice()
    assert inv.payment.bank_account.as_string() == "19-2000145399/0100"


def test_vat_computed_per_rate_not_per_line() -> None:
    data = yaml.safe_load((EXAMPLES / "2605001.yaml").read_text())
    data["supplier"] = load_supplier(EXAMPLES / "supplier.yaml").party().model_dump()
    data["lines"] = [
        {"quantity": 1, "description": f"řádek {i}", "unit_price_net": "12.50", "vat_rate": 21} for i in range(3)
    ]
    inv = Invoice.model_validate(data)
    assert sum(ln.vat_total for ln in inv.lines) == Decimal("7.89")
    assert inv.total_net == Decimal("37.50")
    assert inv.total_vat == Decimal("7.88")
    assert inv.total_gross == Decimal("45.38")
