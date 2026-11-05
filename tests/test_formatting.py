from datetime import date
from decimal import Decimal

from ucetka.formatting import (
    NBSP,
    account_to_iban,
    format_date,
    format_iban,
    format_money,
    format_money_with_currency,
    format_percent,
)


def _iban_valid(iban: str) -> bool:
    """ISO 13616 check: presunout první 4 znaky na konec, písmena → čísla, mod 97 == 1."""
    s = iban[4:] + iban[:4]
    digits = "".join(c if c.isdigit() else str(ord(c.upper()) - ord("A") + 10) for c in s)
    return int(digits) % 97 == 1


def test_format_money_thousands() -> None:
    assert format_money(Decimal("5000.00")) == f"5{NBSP}000,00"
    assert format_money(Decimal("1050.00")) == f"1{NBSP}050,00"
    assert format_money(Decimal("6050.00")) == f"6{NBSP}050,00"


def test_format_money_small() -> None:
    assert format_money(Decimal("12.30")) == "12,30"
    assert format_money(Decimal("0.05")) == "0,05"


def test_format_money_millions() -> None:
    assert format_money(Decimal("1234567.89")) == f"1{NBSP}234{NBSP}567,89"


def test_format_money_with_currency() -> None:
    assert format_money_with_currency(Decimal("6050.00")) == f"6{NBSP}050,00{NBSP}Kč"


def test_format_percent_integer() -> None:
    assert format_percent(Decimal("21")) == f"21{NBSP}%"
    assert format_percent(Decimal("12")) == f"12{NBSP}%"


def test_format_date_czech() -> None:
    assert format_date(date(2026, 5, 15)) == "15. 05. 2026"
    assert format_date(date(2026, 5, 29)) == "29. 05. 2026"


def test_iban_for_reference_account() -> None:
    # 19-2000145399/0100 → BBAN = 0100 000019 2000145399 (20 číslic)
    iban = account_to_iban("19", "2000145399", "0100")
    assert iban == "CZ0801000000192000145399"
    assert len(iban) == 24
    assert _iban_valid(iban)


def test_iban_matches_published_example() -> None:
    # Vzorový český IBAN z registru IBAN (SWIFT) pro účet 19-2000145399/0800
    assert format_iban(account_to_iban("19", "2000145399", "0800")) == "CZ65 0800 0000 1920 0014 5399"


def test_iban_no_prefix() -> None:
    iban = account_to_iban(None, "2000145399", "0100")
    assert len(iban) == 24
    assert iban.startswith("CZ")
    assert iban.endswith("0100" + "000000" + "2000145399")
    assert _iban_valid(iban)


def test_iban_empty_prefix() -> None:
    iban1 = account_to_iban("", "2000145399", "0100")
    iban2 = account_to_iban(None, "2000145399", "0100")
    assert iban1 == iban2


def test_format_iban_spacing() -> None:
    assert format_iban("CZ0801000000192000145399") == "CZ08 0100 0000 1920 0014 5399"
