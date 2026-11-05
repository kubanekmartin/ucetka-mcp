"""Adresář příjemců — načítá customers.yaml a vrací Party podle ID."""
from __future__ import annotations

import re
import unicodedata
from pathlib import Path

import yaml

from ucetka.config import customers_file
from ucetka.models import Party


class CustomerNotFound(KeyError):
    pass


def load_customers(path: Path | None = None) -> dict[str, Party]:
    path = path or customers_file()
    if not path.exists():
        return {}
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return {key: Party.model_validate(data) for key, data in raw.items()}


def get_customer(customer_id: str, *, path: Path | None = None) -> Party:
    path = path or customers_file()
    book = load_customers(path)
    if customer_id not in book:
        raise CustomerNotFound(
            f"Příjemce '{customer_id}' není v {path}. "
            f"Dostupní: {', '.join(sorted(book)) or '(žádní)'}"
        )
    return book[customer_id]


def add_customer(customer_id: str, party: Party) -> Party:
    """Přidá odběratele do adresáře; existující klíč nepřepíše."""
    path = customers_file()
    book = (yaml.safe_load(path.read_text(encoding="utf-8")) if path.exists() else None) or {}
    if customer_id in book:
        raise ValueError(f"'{customer_id}' už v adresáři je")
    book[customer_id] = party.model_dump(exclude_none=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(book, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return party


def slug(name: str) -> str:
    """Klíč do adresáře z názvu firmy: „Vzorová firma s.r.o.“ → „vzorova-firma-s-r-o“."""
    plain = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", "-", plain).strip("-") or "odberatel"
