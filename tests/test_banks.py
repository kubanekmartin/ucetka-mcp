import pytest
from pydantic import ValidationError

from ucetka.banks import bank_bic, bank_name
from ucetka.models import BankAccount


def test_current_banks_present() -> None:
    assert bank_name("6363") == "Partners Banka, a.s."
    assert bank_bic("3060") == "BPKOCZPP"
    assert bank_bic("0100") == "KOMBCZPP"


def test_bank_without_bic_returns_none() -> None:
    assert bank_bic("7960") is None


def test_defunct_bank_code_rejected() -> None:
    with pytest.raises(ValidationError, match="4000"):
        BankAccount(number="2000145399", bank_code="4000")


def test_valid_account_with_prefix() -> None:
    assert BankAccount(prefix="19", number="2000145399", bank_code="0100").as_string() == "19-2000145399/0100"


def test_typo_in_account_number_rejected() -> None:
    with pytest.raises(ValidationError, match="modulo 11"):
        BankAccount(number="2000145398", bank_code="0100")


def test_typo_in_prefix_rejected() -> None:
    with pytest.raises(ValidationError, match="Předčíslí"):
        BankAccount(prefix="18", number="2000145399", bank_code="0100")
