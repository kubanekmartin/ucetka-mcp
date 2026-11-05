from __future__ import annotations

from datetime import date
from decimal import Decimal

NBSP = " "


def format_money(value: Decimal | int | float) -> str:
    """5000.00 -> '5 000,00' (mezera tisíc, čárka desetinné, bez symbolu)."""
    if not isinstance(value, Decimal):
        value = Decimal(str(value))
    q = value.quantize(Decimal("0.01"))
    sign = "-" if q < 0 else ""
    q = abs(q)
    int_part, frac_part = f"{q:.2f}".split(".")
    groups: list[str] = []
    while len(int_part) > 3:
        groups.insert(0, int_part[-3:])
        int_part = int_part[:-3]
    groups.insert(0, int_part)
    return f"{sign}{NBSP.join(groups)},{frac_part}"


def format_money_with_currency(value: Decimal | int | float, currency_symbol: str = "Kč") -> str:
    return f"{format_money(value)}{NBSP}{currency_symbol}"


def format_percent(value: Decimal | int | float) -> str:
    if not isinstance(value, Decimal):
        value = Decimal(str(value))
    q = value.normalize()
    if q == q.to_integral():
        text = f"{int(q)}"
    else:
        text = f"{q}".rstrip("0").rstrip(".")
    return f"{text}{NBSP}%"


def format_date(value: date) -> str:
    return f"{value.day:02d}. {value.month:02d}. {value.year}"


_IBAN_COUNTRY_PREFIX_VALUE = "1235"  # CZ → C=12, Z=35


def account_to_iban(prefix: str | None, number: str, bank_code: str) -> str:
    """České číslo účtu → IBAN.

    BBAN = bankcode(4) + prefix(6, padded) + number(10, padded) — 20 číslic, IBAN má 24 znaků
    IBAN = 'CZ' + checksum(98 - bban#CZ00 mod 97) + BBAN
    """
    p = (prefix or "").rjust(6, "0")
    n = number.rjust(10, "0")
    bban = f"{bank_code}{p}{n}"
    digit_str = f"{bban}{_IBAN_COUNTRY_PREFIX_VALUE}00"
    checksum = 98 - (int(digit_str) % 97)
    return f"CZ{checksum:02d}{bban}"


def format_iban(iban: str) -> str:
    """CZ0801000000192000145399 -> 'CZ08 0100 0000 1920 0014 5399'."""
    chunks = [iban[i : i + 4] for i in range(0, len(iban), 4)]
    return " ".join(chunks)
