import json
from pathlib import Path

import httpx
import pytest

from ucetka import ares

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "ares_87654326.json").read_text())


def _client(status: int = 200, payload: dict | None = None) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(status, json=payload or {})))


def test_lookup_maps_ares_response() -> None:
    party = ares.lookup("87654326", client=_client(payload=FIXTURE))
    assert party.name == "Vzorová firma s.r.o."
    assert party.dic == "CZ87654326"
    assert party.vat_payer
    assert party.address.street == "Vzorová 1234/5"
    assert party.address.city == "Praha 1"
    assert party.address.zip == "110 00"


def test_address_without_street_uses_part_of_town() -> None:
    data = json.loads(json.dumps(FIXTURE))
    sidlo = data["sidlo"]
    for key in ("nazevUlice", "cisloOrientacni", "nazevMestskehoObvodu"):
        sidlo.pop(key)
    sidlo.update(nazevCastiObce="Lhota", nazevObce="Lhota", cisloDomovni=5, psc=28002)
    party = ares.party_from_ares(data)
    assert party.address.street == "Lhota 5"
    assert party.address.zip == "280 02"


def test_unknown_ico_raises() -> None:
    with pytest.raises(ares.CompanyNotFound):
        ares.lookup("12345678", client=_client(status=404))
