"""Lookup firmy v ARES (veřejný REST API Ministerstva financí)."""
from __future__ import annotations

import httpx

from ucetka.models import Address, Party

_URL = "https://ares.gov.cz/ekonomicke-subjekty-v-be/rest/ekonomicke-subjekty/{ico}"


class CompanyNotFound(LookupError):
    pass


def _street(sidlo: dict) -> str:
    number = str(sidlo.get("cisloDomovni", ""))
    if "cisloOrientacni" in sidlo:
        number += f"/{sidlo['cisloOrientacni']}{sidlo.get('cisloOrientacniPismeno', '')}"
    # Obce bez ulic mají adresu "část obce + číslo popisné".
    street = sidlo.get("nazevUlice") or sidlo.get("nazevCastiObce") or sidlo["nazevObce"]
    return f"{street} {number}".strip()


def _zip(psc: int) -> str:
    s = f"{psc:05d}"
    return f"{s[:3]} {s[3:]}"


def party_from_ares(data: dict) -> Party:
    sidlo = data["sidlo"]
    registrations = data.get("seznamRegistraci", {})
    return Party(
        name=data["obchodniJmeno"],
        ico=data["ico"],
        dic=data.get("dic"),
        vat_payer=registrations.get("stavZdrojeDph") == "AKTIVNI",
        address=Address(
            street=_street(sidlo),
            city=sidlo.get("nazevMestskehoObvodu") or sidlo["nazevObce"],
            zip=_zip(sidlo["psc"]),
            country=sidlo.get("nazevStatu", "Česká republika"),
            country_code=sidlo.get("kodStatu", "CZ"),
        ),
    )


def lookup(ico: str, *, client: httpx.Client | None = None) -> Party:
    http = client or httpx.Client(timeout=10)
    response = http.get(_URL.format(ico=ico))
    if response.status_code == 404:
        raise CompanyNotFound(f"IČO {ico} v ARES neexistuje")
    response.raise_for_status()
    return party_from_ares(response.json())
