from pathlib import Path

import yaml

from ucetka.config import load_supplier
from ucetka.models import Invoice
from ucetka.render.qr import make_qr_data_uri, make_qr_png, make_spayd

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"


def _load_invoice() -> Invoice:
    supplier = load_supplier(EXAMPLES / "supplier.yaml").party()
    data = yaml.safe_load((EXAMPLES / "2605001.yaml").read_text())
    data["supplier"] = supplier.model_dump()
    return Invoice.model_validate(data)


def test_spayd_format_for_reference() -> None:
    spayd = make_spayd(_load_invoice())
    # Vstupní účet 19-2000145399/0100 → IBAN CZ08 0100 0000 1920 0014 5399
    assert spayd.startswith("SPD*1.0*")
    assert "ACC:CZ0801000000192000145399" in spayd
    assert "AM:6050.00" in spayd
    assert "CC:CZK" in spayd
    assert "X-VS:2605001" in spayd
    assert "MSG:Faktura 2605001" in spayd


def test_qr_png_bytes() -> None:
    png = make_qr_png(_load_invoice())
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    assert len(png) > 300


def test_qr_data_uri_format() -> None:
    uri = make_qr_data_uri(_load_invoice())
    assert uri.startswith("data:image/png;base64,")
    assert len(uri) > 100


def test_payment_spayd_for_advance() -> None:
    from decimal import Decimal

    from ucetka.models import BankAccount
    from ucetka.render.qr import make_payment_spayd

    spayd = make_payment_spayd(BankAccount.parse("1234567899/0800"), Decimal(3500), "1234567890", "Zaloha OSVC 2026-10")
    assert "ACC:CZ5508000000001234567899" in spayd
    assert "AM:3500.00" in spayd and "CC:CZK" in spayd and "X-VS:1234567890" in spayd
