"""Daňové a pojistné parametry OSVČ podle roku.

Zdroje (ověřeno 2026-10-04):
- DPFO, průměrná mzda, sleva: https://financnisprava.gov.cz/cs/dane/dane/dan-z-prijmu/zamestnanci-zamestnavatele/obecne-informace
- ZDP § 7, § 16, § 38a: https://www.zakonyprolidi.cz/cs/1992-586
- ČSSZ: https://www.cssz.gov.cz/osvc-v-kostce, https://www.cssz.gov.cz/-/prehled-nejdulezitejsich-udaju-pro-socialni-zabezpeceni-v-roce-2026
- VZP: https://www.vzp.cz/o-nas/tiskove-centrum/otazky-tydne/ktere-osvc-nemusi-hradit-zalohy-v-roce-2026
- DPH: https://financnisprava.gov.cz/cs/financni-sprava/novinky/novinky-2025/informace-ke-zmenam-v-platcovstvi-dph-2025
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class TaxParams:
    year: int
    higher_rate_threshold: Decimal
    taxpayer_credit: Decimal
    social_side_threshold: Decimal
    social_side_min_advance: Decimal
    social_max_base: Decimal
    student_health_advances: bool
    income_tax_rate: Decimal = Decimal("0.15")
    higher_income_tax_rate: Decimal = Decimal("0.23")
    flat_rate_expense_cap_60: Decimal = Decimal(1_200_000)
    social_base_share: Decimal = Decimal("0.55")
    social_rate: Decimal = Decimal("0.292")
    health_base_share: Decimal = Decimal("0.50")
    health_rate: Decimal = Decimal("0.135")
    vat_registration_limit: Decimal = Decimal(2_000_000)
    vat_immediate_limit: Decimal = Decimal(2_536_500)


PARAMS: dict[int, TaxParams] = {
    2025: TaxParams(
        year=2025,
        higher_rate_threshold=Decimal(1_676_052),
        taxpayer_credit=Decimal(30_840),
        social_side_threshold=Decimal(111_736),
        social_side_min_advance=Decimal(1_496),
        social_max_base=Decimal(2_234_736),
        student_health_advances=True,
    ),
    2026: TaxParams(
        year=2026,
        higher_rate_threshold=Decimal(1_762_812),
        taxpayer_credit=Decimal(30_840),
        social_side_threshold=Decimal(117_521),
        social_side_min_advance=Decimal(1_574),
        social_max_base=Decimal(2_350_416),
        # Od 2026 student (státní pojištěnec) zálohy na ZP neplatí, jen doplatek po přehledu (zák. 289/2025 Sb.).
        student_health_advances=False,
    ),
}


def params_for(year: int) -> TaxParams:
    if year not in PARAMS:
        raise ValueError(f"Parametry pro rok {year} nejsou zadané v tax_params.py — doplň je z FS/ČSSZ/VZP.")
    return PARAMS[year]
