"""Kurzy ČNB (denní kurzovní lístek) přes https://api.cnb.cz."""
from __future__ import annotations

from datetime import date
from decimal import Decimal

import httpx

_URL = "https://api.cnb.cz/cnbapi/exrates/daily"


def fetch_rate(currency: str, on: date, *, client: httpx.Client | None = None) -> tuple[Decimal, int]:
    """Vrátí (kurz, počet jednotek) platný k danému dni — o víkendu/svátku poslední vyhlášený."""
    http = client or httpx.Client(timeout=10)
    response = http.get(_URL, params={"date": on.isoformat(), "lang": "EN"})
    response.raise_for_status()
    for row in response.json()["rates"]:
        if row["currencyCode"] == currency:
            return Decimal(str(row["rate"])), int(row["amount"])
    raise ValueError(f"ČNB neuvádí kurz pro {currency} k {on.isoformat()}")
