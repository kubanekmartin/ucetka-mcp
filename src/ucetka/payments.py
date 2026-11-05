"""Nastavení daňového profilu (tax.yaml) a evidence zaplacených záloh/doplatků (payments.json)."""
from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ucetka.config import home_dir
from ucetka.models import BankAccount

Kind = Literal["dpfo", "cssz", "zp", "dph"]
KIND_LABELS: dict[str, str] = {
    "dpfo": "Daň z příjmů",
    "cssz": "Sociální pojištění (ČSSZ)",
    "zp": "Zdravotní pojištění",
    "dph": "DPH",
}


class EpoIdentity(BaseModel):
    """Hlavička podání pro EPO (VetaP) — opsaná z posledního podání, ne odvozená ze supplier.yaml."""

    model_config = ConfigDict(extra="forbid")

    c_ufo: str = Field(pattern=r"^\d{3}$", description="Kód finančního úřadu")
    c_pracufo: str = Field(pattern=r"^\d{4}$", description="Kód územního pracoviště")
    dic: str = Field(pattern=r"^\d{8,10}$", description="DIČ bez CZ")
    jmeno: str
    prijmeni: str
    zkrobchjm: str
    ulice: str
    c_pop: str
    naz_obce: str
    psc: str = Field(pattern=r"^\d{5}$")
    email: str | None = None
    c_telef: str | None = None
    c_okec: str = "0"


class AdvancePayment(BaseModel):
    """Platební údaje pro zálohy z dopisu ČSSZ — jen pro QR platbu, nic se nedopočítává."""

    model_config = ConfigDict(extra="forbid")

    account: str = Field(pattern=r"^(\d{1,6}-)?\d{2,10}/\d{4}$", description="prefix-číslo/kód banky")
    variable_symbol: str = Field(pattern=r"^\d{1,10}$")

    @field_validator("account")
    @classmethod
    def _valid_account(cls, value: str) -> str:
        BankAccount.parse(value)
        return value


class TaxSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expense_mode: Literal["flat_rate", "actual"] = Field(
        "flat_rate", description="flat_rate = výdajový paušál, actual = daňová evidence se skutečnými výdaji"
    )
    flat_rate_percent: Literal[30, 40, 60, 80] = 60
    deductions: Decimal = Field(Decimal(0), description="Nezdanitelné části základu (penzijní spoření…)")
    last_tax_liability: Decimal = Field(
        Decimal(0), description="Poslední známá daňová povinnost — určuje zálohy na daň (§ 38a)"
    )
    social_advance_monthly: Decimal = Field(Decimal(0), description="Aktuální měsíční záloha ČSSZ")
    health_advance_monthly: Decimal = Decimal(0)
    side_activity: bool = Field(True, description="Vedlejší činnost — hlavní činnost (minima ČSSZ/ZP) zatím není podporovaná")
    health_status: Literal["student", "employee", "none"] = Field(
        "student",
        description="Proč OSVČ neplatí minimum na zdravotním: student (pojištěnec státu) / employee (zaměstnanec). "
        "none = platí minimum, zatím nepodporováno",
    )
    employment_income: Decimal = Field(
        Decimal(0), description="Roční hrubá mzda (§ 6) — jen pro odhad daně; zálohy z ní strhává zaměstnavatel"
    )
    vat_period: Literal["quarterly", "monthly"] = "quarterly"
    epo: EpoIdentity | None = None
    cssz_payment: AdvancePayment | None = None

    @model_validator(mode="after")
    def _supported_profile(self) -> TaxSettings:
        if not self.side_activity:
            raise ValueError("Hlavní činnost (minimální zálohy ČSSZ a ZP) zatím ucetka nepočítá")
        if self.health_status == "none":
            raise ValueError("health_status none (minimum na zdravotním) zatím ucetka nepočítá")
        return self


class Payment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Kind
    amount: Decimal
    paid_on: date
    tax_year: int
    period: str | None = Field(
        None, pattern=r"^\d{4}-\d{2}$", description="Měsíc zálohy YYYY-MM; prázdné = doplatek / jiná platba"
    )
    note: str | None = None


def settings_file():
    return home_dir() / "tax.yaml"


def payments_file():
    return home_dir() / "payments.json"


def load_settings() -> TaxSettings:
    path = settings_file()
    if not path.exists():
        return TaxSettings()
    return TaxSettings.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")) or {})


def load_payments(tax_year: int | None = None) -> list[Payment]:
    path = payments_file()
    if not path.exists():
        return []
    items = [Payment.model_validate(p) for p in json.loads(path.read_text(encoding="utf-8"))]
    return [p for p in items if tax_year is None or p.tax_year == tax_year]


def record_payment(payment: Payment) -> Payment:
    items = load_payments()
    items.append(payment)
    payments_file().parent.mkdir(parents=True, exist_ok=True)
    payments_file().write_text(
        json.dumps([p.model_dump(mode="json") for p in items], indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    return payment


def paid_total(kind: Kind, tax_year: int) -> Decimal:
    return sum((p.amount for p in load_payments(tax_year) if p.kind == kind), Decimal(0))
