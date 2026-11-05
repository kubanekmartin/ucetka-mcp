from __future__ import annotations

import re
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ucetka.banks import CZECH_BANKS

_PREFIX_WEIGHTS = (10, 5, 8, 4, 2, 1)
_NUMBER_WEIGHTS = (6, 3, 7, 9, 10, 5, 8, 4, 2, 1)


def _mod11_ok(digits: str, weights: tuple[int, ...]) -> bool:
    padded = digits.rjust(len(weights), "0")
    return sum(int(d) * w for d, w in zip(padded, weights)) % 11 == 0


_CURRENCY_SYMBOLS = {"CZK": "Kč", "EUR": "€", "USD": "$", "GBP": "£"}


def _q2(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


_BUILDING_NUMBER_RE = re.compile(r"^(.*?)\s+(\d+(?:/\d+)?[a-zA-Z]?)\s*$")


class Address(BaseModel):
    model_config = ConfigDict(extra="forbid")

    street: str
    building_number: str | None = None
    city: str
    zip: str
    country: str = "Česká republika"
    country_code: str = "CZ"

    @property
    def street_name(self) -> str:
        if self.building_number:
            return self.street
        m = _BUILDING_NUMBER_RE.match(self.street)
        return m.group(1) if m else self.street

    @property
    def building(self) -> str:
        if self.building_number:
            return self.building_number
        m = _BUILDING_NUMBER_RE.match(self.street)
        return m.group(2) if m else ""


class Party(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    ico: str | None = None
    dic: str | None = None
    vat_payer: bool = False
    address: Address
    email: str | None = None
    phone: str | None = None
    web: str | None = None
    registration_note: str | None = Field(
        None, description="Např. 'Fyzická osoba zapsaná v živnostenském rejstříku.' (§ 435 OZ)"
    )

    @field_validator("ico")
    @classmethod
    def _ico_digits(cls, v: str | None) -> str | None:
        if v is None:
            return v
        if not re.fullmatch(r"\d{6,10}", v):
            raise ValueError("IČO musí být 6-10 číslic")
        return v


class BankAccount(BaseModel):
    model_config = ConfigDict(extra="forbid")

    prefix: str | None = None
    number: str
    bank_code: str

    @field_validator("prefix")
    @classmethod
    def _prefix_digits(cls, v: str | None) -> str | None:
        if v is None or v == "":
            return None
        if not re.fullmatch(r"\d{1,6}", v):
            raise ValueError("Prefix účtu musí být 1-6 číslic")
        return v

    @field_validator("number")
    @classmethod
    def _number_digits(cls, v: str) -> str:
        if not re.fullmatch(r"\d{2,10}", v):
            raise ValueError("Číslo účtu musí být 2-10 číslic")
        return v

    @field_validator("bank_code")
    @classmethod
    def _bank_code_digits(cls, v: str) -> str:
        if v not in CZECH_BANKS:
            raise ValueError(f"Kód banky {v} není v číselníku ČNB")
        return v

    @model_validator(mode="after")
    def _checksum(self) -> BankAccount:
        if self.prefix and not _mod11_ok(self.prefix, _PREFIX_WEIGHTS):
            raise ValueError(f"Předčíslí {self.prefix} nesplňuje kontrolu modulo 11 (překlep?)")
        if not _mod11_ok(self.number, _NUMBER_WEIGHTS):
            raise ValueError(f"Číslo účtu {self.number} nesplňuje kontrolu modulo 11 (překlep?)")
        return self

    @classmethod
    def parse(cls, text: str) -> BankAccount:
        if "/" not in text:
            raise ValueError("Účet musí být ve formátu '[předčíslí-]číslo/kód banky', např. '2000145399/0100'.")
        acc_part, bank_code = text.rsplit("/", 1)
        prefix, number = acc_part.split("-", 1) if "-" in acc_part else (None, acc_part)
        return cls(prefix=prefix, number=number, bank_code=bank_code)

    def as_string(self) -> str:
        if self.prefix:
            return f"{self.prefix}-{self.number}/{self.bank_code}"
        return f"{self.number}/{self.bank_code}"


class SupplierConfig(Party):
    """Obsah supplier.yaml: dodavatel + výchozí účet pro platby."""

    bank_account: BankAccount | None = None

    def party(self) -> Party:
        return Party.model_validate(self.model_dump(exclude={"bank_account"}))


class LineItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    quantity: Decimal
    unit: str = "ks"
    description: str
    unit_price_net: Decimal
    vat_rate: Decimal

    @property
    def net_total(self) -> Decimal:
        return _q2(self.quantity * self.unit_price_net)

    @property
    def vat_total(self) -> Decimal:
        return _q2(self.net_total * self.vat_rate / Decimal(100))

    @property
    def gross_total(self) -> Decimal:
        return _q2(self.net_total + self.vat_total)


class VatBreakdownRow(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rate: Decimal
    base: Decimal
    vat: Decimal
    total: Decimal
    base_czk: Decimal
    vat_czk: Decimal


class PaymentInfo(BaseModel):
    model_config = ConfigDict(extra="forbid")

    bank_account: BankAccount | None = Field(
        None, description="Povinné pro vystavení; importované faktury placené kartou/marketplace ho nemají."
    )
    payment_form: str = "Převodem"
    variable_symbol: str
    constant_symbol: str | None = None
    specific_symbol: str | None = None


class OriginalDocument(BaseModel):
    model_config = ConfigDict(extra="forbid")

    number: str
    issue_date: date


class Invoice(BaseModel):
    model_config = ConfigDict(extra="forbid")

    document_type: Literal["invoice", "credit_note"] = "invoice"
    original: OriginalDocument | None = Field(None, description="Opravovaný doklad (jen dobropis)")
    correction_reason: str | None = Field(None, description="Důvod opravy (§ 45 ZDPH, jen dobropis)")
    number: str = Field(..., pattern=r"^[A-Za-z0-9_-]{1,20}$", description="Evidenční číslo dokladu")

    issue_date: date
    tax_point_date: date
    due_date: date

    supplier: Party
    customer: Party

    payment: PaymentInfo

    currency: str = Field("CZK", pattern=r"^[A-Z]{3}$")
    exchange_rate: Decimal | None = Field(
        None, gt=0, description="Kurz ČNB k DUZP: kolik Kč stojí `exchange_rate_amount` jednotek měny faktury."
    )
    exchange_rate_amount: int = Field(1, gt=0)

    lines: list[LineItem] = Field(min_length=1)

    issued_by: str | None = None
    note: str | None = None
    language: Literal["cs", "en"] = Field("cs", description="Jazyk PDF faktury; ISDOC a evidence zůstávají česky.")
    reverse_charge: bool = Field(
        False, description="Služba osobě registrované k DPH v jiném státě EU (§ 9 odst. 1, daň odvede zákazník)."
    )

    @model_validator(mode="after")
    def _check_dates(self) -> Invoice:
        if self.due_date < self.issue_date:
            raise ValueError("Datum splatnosti nemůže být před datem vystavení")
        return self

    @model_validator(mode="after")
    def _exchange_rate_for_foreign_currency(self) -> Invoice:
        if self.is_foreign_currency and self.exchange_rate is None:
            raise ValueError(f"Faktura v {self.currency} potřebuje exchange_rate (kurz ČNB k DUZP)")
        if not self.is_foreign_currency and self.exchange_rate is not None:
            raise ValueError("Faktura v CZK nesmí mít exchange_rate")
        return self

    @model_validator(mode="after")
    def _non_payer_has_no_vat(self) -> Invoice:
        if not self.supplier.vat_payer and any(ln.vat_rate != 0 for ln in self.lines):
            raise ValueError("Neplátce DPH nesmí účtovat DPH — všechny položky musí mít vat_rate 0")
        return self

    @model_validator(mode="after")
    def _reverse_charge_rules(self) -> Invoice:
        if not self.reverse_charge:
            return self
        if not self.supplier.dic:
            raise ValueError("Reverse charge vyžaduje DIČ dodavatele")
        if not self.customer.dic or self.customer.dic.upper().startswith("CZ"):
            raise ValueError("Reverse charge vyžaduje zahraniční EU DIČ odběratele (ne CZ)")
        if self.customer.address.country_code == "CZ":
            raise ValueError("Reverse charge: odběratel musí mít sídlo mimo ČR")
        if any(ln.vat_rate != 0 for ln in self.lines):
            raise ValueError("Reverse charge: všechny položky musí mít vat_rate 0 — daň odvede zákazník")
        return self

    @model_validator(mode="after")
    def _credit_note_rules(self) -> Invoice:
        is_credit = self.document_type == "credit_note"
        if is_credit and (self.original is None or not self.correction_reason):
            raise ValueError("Dobropis potřebuje original (číslo a datum původní faktury) a correction_reason")
        if not is_credit and (self.original is not None or self.correction_reason):
            raise ValueError("original a correction_reason patří jen k dobropisu")
        return self

    @property
    def is_credit_note(self) -> bool:
        return self.document_type == "credit_note"

    @property
    def sign(self) -> int:
        """Dobropis má na dokladu (i v ISDOC) kladné částky; v součtech se odečítá."""
        return -1 if self.is_credit_note else 1

    @property
    def is_foreign_currency(self) -> bool:
        return self.currency != "CZK"

    @property
    def currency_symbol(self) -> str:
        return _CURRENCY_SYMBOLS.get(self.currency, self.currency)

    def to_czk(self, amount: Decimal) -> Decimal:
        if not self.is_foreign_currency:
            return amount
        return _q2(amount * self.exchange_rate / self.exchange_rate_amount)

    @property
    def is_tax_document(self) -> bool:
        return self.supplier.vat_payer or self.reverse_charge

    @property
    def legal_notes(self) -> list[str]:
        notes: list[str] = []
        if not self.supplier.vat_payer and not self.reverse_charge:
            notes.append("Nejsem plátce DPH.")
        if self.reverse_charge:
            notes.append("Daň odvede zákazník. Reverse charge — čl. 196 směrnice 2006/112/ES.")
        return notes

    @property
    def title(self) -> str:
        if self.is_credit_note:
            return "Opravný daňový doklad — dobropis" if self.is_tax_document else "Dobropis"
        return "Faktura — daňový doklad" if self.is_tax_document else "Faktura"

    def vat_breakdown(self) -> list[VatBreakdownRow]:
        """Daň za každou sazbu se počítá ze součtu základů (§ 37 ZDPH), ne součtem zaokrouhlených řádků."""
        bases: dict[Decimal, Decimal] = {}
        for line in self.lines:
            bases[line.vat_rate] = bases.get(line.vat_rate, Decimal(0)) + line.net_total
        rows: list[VatBreakdownRow] = []
        for rate in sorted(bases):
            base = _q2(bases[rate])
            vat = _q2(base * rate / Decimal(100))
            base_czk = self.to_czk(base)
            rows.append(
                VatBreakdownRow(
                    rate=rate,
                    base=base,
                    vat=vat,
                    total=base + vat,
                    base_czk=base_czk,
                    vat_czk=_q2(base_czk * rate / Decimal(100)),
                )
            )
        return rows

    @property
    def total_net(self) -> Decimal:
        return _q2(sum((ln.net_total for ln in self.lines), Decimal(0)))

    @property
    def total_vat(self) -> Decimal:
        return sum((row.vat for row in self.vat_breakdown()), Decimal(0))

    @property
    def total_gross(self) -> Decimal:
        return _q2(self.total_net + self.total_vat)

    @property
    def total_net_czk(self) -> Decimal:
        return sum((row.base_czk for row in self.vat_breakdown()), Decimal(0))

    @property
    def total_vat_czk(self) -> Decimal:
        """DPH se v Kč počítá ze základu přepočteného kurzem ČNB (§ 4 odst. 5, § 37 ZDPH)."""
        return sum((row.vat_czk for row in self.vat_breakdown()), Decimal(0))
