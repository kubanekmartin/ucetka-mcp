from decimal import Decimal
from pathlib import Path

import pytest
import yaml

from ucetka.config import load_supplier
from ucetka.models import Invoice, LineItem
from ucetka.render.qr import make_qr_png, make_spayd
from ucetka.render.sind import make_sind, sind_for_spayd

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"


def _load_invoice() -> Invoice:
    supplier = load_supplier(EXAMPLES / "supplier.yaml").party()
    data = yaml.safe_load((EXAMPLES / "2605001.yaml").read_text())
    data["supplier"] = supplier.model_dump()
    return Invoice.model_validate(data)


def test_sind_exact_string() -> None:
    assert make_sind(_load_invoice()) == (
        "SID*1.0*ID:2605001*DD:20260515*AM:6050.00*VS:2605001"
        "*VII:CZ98765434*INI:98765434*VIR:CZ87654326*INR:87654326"
        "*DUZP:20260515*DT:20260529*TB0:5000.00*T0:1050.00"
        "*CC:CZK*ACC:CZ0801000000192000145399"
    )


def test_sind_reduced_rate_and_zero_rate() -> None:
    inv = _load_invoice().model_copy(
        update={
            "lines": [
                LineItem(quantity=Decimal(1), description="a", unit_price_net=Decimal(1000), vat_rate=Decimal(21)),
                LineItem(quantity=Decimal(1), description="b", unit_price_net=Decimal(200), vat_rate=Decimal(12)),
                LineItem(quantity=Decimal(1), description="c", unit_price_net=Decimal(50), vat_rate=Decimal(0)),
            ]
        }
    )
    sind = make_sind(inv)
    assert "*TB0:1000.00*T0:210.00*TB1:200.00*T1:24.00*NTB:50.00*" in sind
    assert "TB2" not in sind


def test_sind_non_vat_payer_uses_ntb() -> None:
    inv = _load_invoice()
    inv = inv.model_copy(update={"supplier": inv.supplier.model_copy(update={"vat_payer": False, "dic": None})})
    sind = make_sind(inv)
    assert "*NTB:5000.00*" in sind
    assert "TB0" not in sind


def test_sind_unknown_rate_raises() -> None:
    inv = _load_invoice()
    line = inv.lines[0].model_copy(update={"vat_rate": Decimal(15)})
    with pytest.raises(ValueError, match="15"):
        make_sind(inv.model_copy(update={"lines": [line]}))


def test_x_inv_omits_spayd_keys_and_encodes_only_asterisk() -> None:
    assert sind_for_spayd(_load_invoice()) == (
        "SID%2A1.0%2AID:2605001%2ADD:20260515"
        "%2AVII:CZ98765434%2AINI:98765434%2AVIR:CZ87654326%2AINR:87654326"
        "%2ADUZP:20260515%2ATB0:5000.00%2AT0:1050.00"
    )


def test_spayd_with_invoice_contains_x_inv() -> None:
    spayd = make_spayd(_load_invoice(), with_invoice=True)
    assert spayd.endswith("*X-INV:" + sind_for_spayd(_load_invoice()))
    assert spayd.count("AM:") == 1


def test_qr_png_with_invoice_is_valid_png() -> None:
    png = make_qr_png(_load_invoice(), with_invoice=True)
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    assert len(png) > 500
