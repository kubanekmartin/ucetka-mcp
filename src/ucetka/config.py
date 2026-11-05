from __future__ import annotations

import os
from pathlib import Path

import yaml

from ucetka.models import SupplierConfig


def home_dir() -> Path:
    return Path(os.environ.get("UCETKA_HOME", Path.home() / ".ucetka"))


def supplier_file() -> Path:
    return home_dir() / "supplier.yaml"


def customers_file() -> Path:
    return home_dir() / "customers.yaml"


def state_file() -> Path:
    """Starý counter čísel faktur před ledgerem — čte se jen jako spodní mez číslování."""
    return home_dir() / "state.json"


def invoices_dir() -> Path:
    return home_dir() / "invoices"


def output_dir() -> Path:
    return Path(os.environ.get("UCETKA_OUT", Path.home() / "Documents" / "Účetnictví" / "Vydané faktury"))


def received_dir() -> Path:
    return Path(os.environ.get("UCETKA_RECEIVED", Path.home() / "Documents" / "Účetnictví" / "Přijaté faktury"))


def reports_dir() -> Path:
    return Path(os.environ.get("UCETKA_REPORTS", Path.home() / "Documents" / "Účetnictví" / "Přehledy"))


def expenses_dir() -> Path:
    return home_dir() / "expenses"


def load_supplier(path: Path | None = None) -> SupplierConfig:
    path = path or supplier_file()
    if not path.exists():
        raise FileNotFoundError(f"Chybí supplier config: {path}. Vytvoř ho podle examples/supplier.yaml.")
    return SupplierConfig.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
