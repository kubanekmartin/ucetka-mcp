"""První spuštění: z odpovědí průvodce `ucetka init` zapíše supplier.yaml a tax.yaml a založí složky."""
from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import yaml

from ucetka.config import home_dir, output_dir, received_dir, reports_dir, supplier_file
from ucetka.models import BankAccount, Party, SupplierConfig
from ucetka.payments import TaxSettings, settings_file


class AlreadyConfigured(RuntimeError):
    pass


def write_profile(
    party: Party,
    account: BankAccount,
    *,
    vat_period: str,
    expense_mode: str,
    flat_rate_percent: int,
    health_status: str,
    employment_income: Decimal,
    social_advance_monthly: Decimal,
) -> list[Path]:
    """Validuje vše přes modely dřív, než něco zapíše — rozbitý profil na disk nepustí."""
    existing = [p for p in (supplier_file(), settings_file()) if p.exists()]
    if existing:
        raise AlreadyConfigured("Profil už existuje: " + ", ".join(map(str, existing)))
    supplier = SupplierConfig.model_validate(party.model_dump() | {"bank_account": account.model_dump()})
    settings = TaxSettings(
        vat_period=vat_period,
        expense_mode=expense_mode,
        flat_rate_percent=flat_rate_percent,
        health_status=health_status,
        employment_income=employment_income,
        social_advance_monthly=social_advance_monthly,
    )
    home_dir().mkdir(parents=True, exist_ok=True)
    supplier_file().write_text(
        yaml.safe_dump(supplier.model_dump(mode="json", exclude_none=True), allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    settings_file().write_text(
        "# Daňový profil ucetka. Hlavičku pro EPO (epo:) doplň podle README před prvním exportem DPH.\n"
        + yaml.safe_dump(settings.model_dump(mode="json", exclude_none=True), allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    folders = [output_dir(), received_dir(), reports_dir()]
    for folder in folders:
        folder.mkdir(parents=True, exist_ok=True)
    return [supplier_file(), settings_file(), *folders]
