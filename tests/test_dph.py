from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
import yaml
from lxml import etree
from ucetka import expenses, ledger
from ucetka.config import load_supplier
from ucetka.export import dph
from ucetka.models import Invoice

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "epo"

EPO = {
    "c_ufo": "451",
    "c_pracufo": "2001",
    "dic": "98765434",
    "jmeno": "Petr",
    "prijmeni": "Svoboda",
    "zkrobchjm": "PETR SVOBODA",
    "ulice": "Hlavní",
    "c_pop": "123",
    "naz_obce": "Kolín",
    "psc": "28002",
    "email": "test@example.com",
    "c_telef": "700000000",
    "c_okec": "0",
}


@pytest.fixture(autouse=True)
def _epo(ucetka_home: Path) -> None:
    (ucetka_home / "tax.yaml").write_text(
        yaml.safe_dump({"epo": EPO}, allow_unicode=True)
    )


def _invoice(
    number: str, duzp: date, net: str, customer_dic: str = "CZ87654326", **changes
) -> Invoice:
    data = yaml.safe_load((EXAMPLES / "2605001.yaml").read_text())
    data["supplier"] = load_supplier(EXAMPLES / "supplier.yaml").party().model_dump()
    data["customer"]["dic"] = customer_dic
    data |= {
        "number": number,
        "issue_date": duzp,
        "tax_point_date": duzp,
        "due_date": duzp,
    }
    data["lines"][0]["unit_price_net"] = net
    data |= changes
    return Invoice.model_validate(data)


def _expense(id_: str, doc: str, duzp: date, base: str, vat: str, **kw) -> None:
    expenses.save(
        expenses.Expense(
            id=id_,
            document_number=doc,
            supplier_name=kw.pop("supplier_name", "Dodavatel"),
            issue_date=duzp,
            tax_point_date=duzp,
            vat_rows=[
                expenses.VatRow(rate=Decimal(21), base=Decimal(base), vat=Decimal(vat))
            ],
            total=Decimal(base) + Decimal(vat),
            **kw,
        )
    )


def _setup_q2_2026() -> None:
    """Smyšlené doklady pokrývající stejné řádky jako skutečné podání v EPO (A.2 EU i mimo EU, A.4, A.5, B.2, B.3)."""
    for number, duzp, net in [
        ("2604001", date(2026, 4, 30), "42000"),
        ("2605002", date(2026, 5, 31), "38500"),
        ("2606001", date(2026, 6, 30), "45200"),
        ("2605001", date(2026, 5, 15), "5000"),
    ]:
        ledger.save(_invoice(number, duzp, net), Path(f"{number}.pdf"))
    eu_cloud = {
        "supplier_dic": "IE1234567T",
        "supplier_country": "IE",
        "reverse_charge": True,
    }
    us_saas = {"supplier_country": "US", "reverse_charge": True}
    for i, (doc, duzp, base, vat, kw) in enumerate(
        [
            ("INV-2026-0401", date(2026, 4, 1), "250.40", "52.58", eu_cloud),
            (
                "77/cz/czk/2026/03",
                date(2026, 4, 1),
                "45.10",
                "9.47",
                {
                    "supplier_dic": "PL1234567890",
                    "supplier_country": "PL",
                    "reverse_charge": True,
                },
            ),
            ("INV-2026-0430", date(2026, 4, 30), "310.00", "65.10", eu_cloud),
            ("US-0003", date(2026, 5, 7), "1950.00", "409.50", us_saas),
            ("INV-2026-0531", date(2026, 5, 31), "405.20", "85.09", eu_cloud),
            ("us-0004", date(2026, 6, 7), "1980.60", "415.93", us_saas),
            ("INV-2026-0630", date(2026, 6, 30), "690.75", "145.06", eu_cloud),
        ]
    ):
        _expense(f"a2-{i}", doc, duzp, base, vat, **kw)
    cz = {"supplier_dic": "CZ99887762"}
    _expense("b2-1", "FV-2026-118", date(2026, 6, 2), "8900", "1869", **cz)
    _expense("b2-2", "FV-2026-131", date(2026, 6, 23), "15400", "3234", **cz)
    _expense("b3-1", "FV-2026-074", date(2026, 4, 30), "3800", "798", **cz)
    _expense("b3-2", "FV-2026-096", date(2026, 5, 31), "1600", "336", **cz)


def _vety(xml: bytes, root_tag: str) -> list[tuple[str, dict[str, str]]]:
    doc = etree.fromstring(xml).find(root_tag)
    return [(el.tag, dict(el.attrib)) for el in doc if el.tag not in ("VetaD",)]


def test_q2_2026_return_matches_epo_snapshot() -> None:
    _setup_q2_2026()
    ret = dph.collect(2026, 2)
    ours = _vety(dph.render_dp3(ret, date(2026, 7, 26)), "DPHDP3")
    expected = _vety((FIXTURES / "DPHDP3_2026Q2.xml").read_bytes(), "DPHDP3")
    assert ours == expected


def test_q2_2026_control_statement_matches_epo_snapshot() -> None:
    _setup_q2_2026()
    ret = dph.collect(2026, 2)
    ours = _vety(dph.render_kh(ret, date(2026, 7, 26)), "DPHKH1")
    expected = _vety((FIXTURES / "DPHKH1_2026Q2.xml").read_bytes(), "DPHKH1")
    assert ours == expected


def test_cancelled_and_other_quarters_are_ignored() -> None:
    ledger.save(_invoice("2607001", date(2026, 7, 31), "1000"), Path("a.pdf"))
    ledger.save(_invoice("2606009", date(2026, 6, 30), "99999"), Path("b.pdf"))
    ledger.save(_invoice("2608001", date(2026, 8, 1), "2000"), Path("c.pdf"))
    ledger.cancel("2608001", "chyba")
    lines = dph.dp3_lines(dph.collect(2026, 3))
    assert lines["Veta1"]["obrat23"] == 1000
    assert lines["Veta6"]["dano_da"] == 210


def test_small_invoice_and_non_payer_customer_go_to_a5() -> None:
    ledger.save(
        _invoice("2607001", date(2026, 7, 31), "50000", customer_dic=None),
        Path("a.pdf"),
    )
    ledger.save(_invoice("2607002", date(2026, 7, 31), "8000"), Path("b.pdf"))
    ret = dph.collect(2026, 3)
    assert ret.a4 == []
    assert ret.a5[Decimal(21)] == [Decimal("58000.00"), Decimal("12180.00")]


def test_reverse_charge_needs_supplier_country() -> None:
    with pytest.raises(ValueError, match="supplier_country"):
        _expense(
            "x",
            "1",
            date(2026, 7, 1),
            "100",
            "21",
            reverse_charge=True,
            supplier_dic="IE1",
        )


def test_us_supplier_with_eu_vat_id_is_third_country() -> None:
    _expense(
        "x",
        "US-0006",
        date(2026, 8, 7),
        "2050.00",
        "430.50",
        reverse_charge=True,
        supplier_dic="IE7654321W",
        supplier_country="US",
    )
    ret = dph.collect(2026, 3)
    lines = dph.dp3_lines(ret)
    assert lines["Veta1"]["p_sl23_z"] == 2050
    assert lines["Veta1"]["p_sl23_e"] == 0
    assert ret.a2[0].country is None


def test_eu_goods_go_to_line_3() -> None:
    _expense(
        "x",
        "PL-2026-118",
        date(2026, 3, 2),
        "1200.40",
        "252.08",
        reverse_charge=True,
        supplier_dic="PL1234567890",
        supplier_country="PL",
        goods=True,
    )
    lines = dph.dp3_lines(dph.collect(2026, 1))
    assert (lines["Veta1"]["p_zb23"], lines["Veta1"]["dan_pzb23"]) == (1200, 252)
    assert lines["Veta4"]["nar_zdp23"] == 1200


def test_export_writes_both_files(ucetka_home: Path) -> None:
    _setup_q2_2026()
    paths, _ = dph.export(2026, 2, filed_on=date(2026, 7, 26))
    assert [p.parent.name for p in paths] == ["DPH", "KH"]
    assert paths[0].name.startswith("DPHDP3-98765434-")
    assert etree.fromstring(paths[1].read_bytes()).get("nazevSW") == "ucetka-mcp"


def _eu_invoice(number: str, dic: str, net: str) -> Invoice:
    data = yaml.safe_load((EXAMPLES / "eu_customer.yaml").read_text())
    data["supplier"] = load_supplier(EXAMPLES / "supplier.yaml").party().model_dump()
    data["customer"]["dic"] = dic
    data |= {
        "number": number,
        "issue_date": date(2026, 8, 31),
        "tax_point_date": date(2026, 8, 31),
        "due_date": date(2026, 9, 14),
    }
    data["lines"] = [data["lines"][0] | {"unit_price_net": net, "quantity": 1}]
    return Invoice.model_validate(data)


def test_eu_services_go_to_line_21_and_recapitulative_statement() -> None:
    ledger.save(_eu_invoice("2608001", "DE123456789", "1000.40"), Path("a.pdf"))
    ledger.save(_eu_invoice("2608002", "DE123456789", "500"), Path("b.pdf"))
    ledger.save(_eu_invoice("2608003", "ATU12345678", "300"), Path("c.pdf"))
    ret = dph.collect(2026, 3)
    assert dph.dp3_lines(ret)["Veta2"]["pln_sluzby"] == 1800
    assert ret.a4 == [] and ret.a5 == {}

    shv = etree.fromstring(dph.render_shv(ret, date(2026, 10, 20))).find("DPHSHV")
    assert shv.get("verzePis") == "02.01.05"
    assert shv.find("VetaD").attrib == {
        "dokument": "SHV",
        "k_uladis": "DPH",
        "rok": "2026",
        "ctvrt": "3",
        "shvies_forma": "R",
        "d_poddp": "20.10.2026",
    }
    rows = [dict(r.attrib) for r in shv.findall("VetaR")]
    assert [
        (r["k_stat"], r["c_vat"], r["k_pln_eu"], r["pln_hodnota"], r["pln_pocet"])
        for r in rows
    ] == [
        ("AT", "U12345678", "3", "300", "1"),
        ("DE", "123456789", "3", "1500", "2"),
    ]
    paths, _ = dph.export(2026, 3, filed_on=date(2026, 10, 20))
    assert [p.parent.name for p in paths] == ["DPH", "KH", "SH"]


def test_no_eu_services_no_recapitulative_statement() -> None:
    with pytest.raises(dph.VatExportError, match="nepodává"):
        dph.render_shv(dph.collect(2026, 3), date(2026, 10, 20))


def test_missing_epo_identity_fails(ucetka_home: Path) -> None:
    (ucetka_home / "tax.yaml").write_text("vat_period: quarterly\n")
    with pytest.raises(dph.VatExportError, match="epo"):
        dph.render_dp3(dph.collect(2026, 3), date(2026, 10, 20))


def test_late_document_claimed_in_later_quarter_keeps_original_dppd() -> None:
    _expense(
        "late",
        "FV-2026-140",
        date(2026, 6, 27),
        "18000.00",
        "3780.00",
        supplier_dic="CZ99887762",
        claim_date=date(2026, 8, 9),
    )
    assert dph.collect(2026, 2).b2 == []
    ret = dph.collect(2026, 3)
    assert dph.dp3_lines(ret)["Veta4"]["odp_tuz23_nar"] == 3780
    kh = etree.fromstring(dph.render_kh(ret, date(2026, 10, 20))).find("DPHKH1/VetaB2")
    assert kh.get("dppd") == "27.06.2026"


def test_claim_date_not_allowed_for_reverse_charge() -> None:
    with pytest.raises(ValueError, match="claim_date"):
        _expense(
            "x",
            "1",
            date(2026, 6, 30),
            "100",
            "21",
            reverse_charge=True,
            supplier_dic="IE1",
            supplier_country="IE",
            claim_date=date(2026, 8, 1),
        )


def test_partial_business_use_with_vat_is_refused() -> None:
    _expense(
        "phone",
        "A1",
        date(2026, 9, 18),
        "30000",
        "6300",
        supplier_dic="CZ99887762",
        business_share=50,
    )
    with pytest.raises(dph.VatExportError, match="§ 75"):
        dph.collect(2026, 3)
