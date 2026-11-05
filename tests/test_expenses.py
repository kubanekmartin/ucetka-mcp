from decimal import Decimal
from pathlib import Path

import pytest

from ucetka import expenses

ALZA_TEXT = """Faktura - Daňový doklad - 4000000001
záruční a dodací list -
Prodávající: Alza.cz a.s.
Jankovcova 1522/53, 17000 Praha 7, IC: 27082440, DIC: CZ27082440, Internet: www.alza.cz
Datum vystavení: 22.01.2026
Datum uskut. zdaň. plnění: 21.01.2026
Kód Popis Ks Cena ks bez DPH DPH DPH% Cena Záruka
X1 Notebook 1 12 815,38 12 815,38 2 691,23 21 15 506,61 24
B1 Kniha 1 100,00 100,00 12,00 12 112,00 0
Celkem: 15 618,61 Kč
Zaokrouhlení: 0,39 Kč
Sazba Základ DPH
Celkem: 15 619,00 Kč
21 % 12 815,38 2 691,23
12 % 100,00 12,00
Vyčíslení DPH v Kč: Kurs: 1,000
Sazba Základ DPH
21 % 12 815,38 2 691,23
"""


def test_parse_alza_vat_recap() -> None:
    e = expenses.parse_alza(ALZA_TEXT, "2601001")
    assert e.document_number == "4000000001"
    assert e.supplier_ico == "27082440"
    assert str(e.tax_point_date) == "2026-01-21"
    assert [(r.rate, r.base, r.vat) for r in e.vat_rows] == [
        (Decimal(21), Decimal("12815.38"), Decimal("2691.23")),
        (Decimal(12), Decimal("100.00"), Decimal("12.00")),
    ]
    assert e.total == Decimal("15619.00")


def test_parse_alza_rejects_inconsistent_total() -> None:
    with pytest.raises(expenses.ParseFailed, match="nesedí"):
        expenses.parse_alza(ALZA_TEXT.replace("Celkem: 15 619,00 Kč", "Celkem: 15 700,00 Kč"), "x")


def test_unknown_supplier_is_flagged() -> None:
    with pytest.raises(expenses.ParseFailed, match="bez ISDOC"):
        expenses.parse_alza("Google Cloud EMEA Limited invoice", "x")


def test_sync_flags_unreadable_pdf(tmp_path: Path) -> None:
    folder = tmp_path / "prijate" / "26_03_prijate"
    folder.mkdir(parents=True)
    (folder / "2603002.pdf").write_bytes(_blank_pdf())
    assert expenses.sync(tmp_path / "prijate", since_year=2025) == []
    assert expenses.load_issues()[0].pdf_path.name == "2603002.pdf"


def _blank_pdf() -> bytes:
    import io

    import pikepdf

    pdf = pikepdf.new()
    pdf.add_blank_page()
    buf = io.BytesIO()
    pdf.save(buf)
    return buf.getvalue()


def test_alza_card_payment_is_paid_on_issue_date() -> None:
    assert expenses.parse_alza(ALZA_TEXT, "a").paid_date is None
    paid = expenses.parse_alza(ALZA_TEXT.replace("Datum vystavení:", "Způsob úhrady: Apple Pay\nDatum vystavení:"), "a")
    assert str(paid.paid_date) == "2026-01-22"


def _e(**kw) -> expenses.Expense:
    base = dict(id="x", document_number="1", supplier_name="S", issue_date="2026-03-01", tax_point_date="2026-03-01",
                vat_rows=[expenses.VatRow(rate=Decimal(21), base=Decimal(1000), vat=Decimal(210))], total=Decimal(1210))
    return expenses.Expense(**(base | kw))


@pytest.mark.parametrize(
    ("kw", "vat_payer", "cost"),
    [
        ({}, True, "1000.00"),
        ({}, False, "1210.00"),
        ({"business_share": 60}, True, "600.00"),
        ({"tax_deductible": False}, True, "0"),
        ({"vat_rows": [], "total": Decimal(500)}, True, "500.00"),
        ({"reverse_charge": True, "supplier_country": "IE", "total": Decimal(1000)}, True, "1000.00"),
    ],
)
def test_income_tax_cost(kw: dict, vat_payer: bool, cost: str) -> None:
    assert expenses.income_tax_cost(_e(**kw), vat_payer=vat_payer) == Decimal(cost)


def test_mark_paid_once() -> None:
    expenses.save(_e())
    assert str(expenses.mark_paid("x", __import__("datetime").date(2026, 3, 5)).paid_date) == "2026-03-05"
    with pytest.raises(ValueError, match="už je zaplacený"):
        expenses.mark_paid("x", __import__("datetime").date(2026, 3, 6))


EXAMPLES = Path(__file__).resolve().parents[1] / "examples"


def _received_isdoc(customer_ico: str = "98765434", country: str = "CZ") -> bytes:
    """Faktura od Vzorové firmy pro dodavatele z examples (= „já“), vyrenderovaná naším ISDOC generátorem."""
    import yaml

    from ucetka.config import load_supplier
    from ucetka.isdoc.generator import render_isdoc
    from ucetka.models import Invoice

    me = load_supplier(EXAMPLES / "supplier.yaml").party().model_dump()
    data = yaml.safe_load((EXAMPLES / "2605001.yaml").read_text())
    vendor = data["customer"] | {"address": data["customer"]["address"] | {"country_code": country}}
    data |= {"supplier": vendor, "customer": me | {"ico": customer_ico}, "number": "FV-2026-0042"}
    data["lines"].append({"quantity": 1, "unit": "ks", "description": "Kniha", "unit_price_net": 100, "vat_rate": 12})
    return render_isdoc(Invoice.model_validate(data))


def _pdf_with_attachment(xml: bytes, name: str) -> bytes:
    import io

    import pikepdf

    pdf = pikepdf.new()
    pdf.add_blank_page()
    pdf.attachments[name] = pikepdf.AttachedFileSpec(pdf, xml, mime_type="text/xml")
    buf = io.BytesIO()
    pdf.save(buf)
    return buf.getvalue()


def test_sync_reads_isdoc_first_even_with_pohoda_attachment_name(tmp_path: Path) -> None:
    folder = tmp_path / "prijate" / "26_05_prijate"
    folder.mkdir(parents=True)
    # Pohoda ukládá název přílohy s escapovanou tečkou a cestou z Windows.
    pdf = _pdf_with_attachment(_received_isdoc(), r"C\/\/Users\/x\/Temp\/invoice2Eisdoc")
    (folder / "2605003.pdf").write_bytes(pdf)
    assert expenses.sync(tmp_path / "prijate", since_year=2025) == ["2605003"]
    e = expenses.list_expenses()[0]
    assert (e.supplier_name, e.supplier_ico, e.document_number) == ("Vzorová firma s.r.o.", "87654326", "FV-2026-0042")
    assert [(r.rate, r.base, r.vat) for r in e.vat_rows] == [
        (Decimal(21), Decimal("5000.00"), Decimal("1050.00")),
        (Decimal(12), Decimal("100.00"), Decimal("12.00")),
    ]
    assert e.total == Decimal("6162.00")
    assert expenses.load_issues() == []


@pytest.mark.parametrize(
    ("kw", "reason"),
    [({"customer_ico": "11111111"}, "ne na tvoje"), ({"country": "DE"}, "Zahraniční dodavatel")],
)
def test_isdoc_that_needs_a_human_or_ai_is_flagged(kw: dict, reason: str) -> None:
    with pytest.raises(expenses.ParseFailed, match=reason):
        expenses.parse_isdoc_expense(_received_isdoc(**kw), "x", own_ico="98765434")


def _alza_style(xml: bytes) -> bytes:
    """Alza píše sazbu a částky na 4 desetinná místa („21.0000“, „5000.0000“)."""
    import re

    out = re.sub(rb"<Percent>(\d+)</Percent>", rb"<Percent>\1.0000</Percent>", xml)
    out = re.sub(rb">(\d+\.\d{2})</", rb">\g<1>00</", out)
    assert out.count(b"21.0000") and out.count(b"5000.0000")
    return out


def test_standalone_isdoc_without_pdf_is_imported_and_normalized(tmp_path: Path) -> None:
    folder = tmp_path / "prijate" / "26_11_prijate"
    folder.mkdir(parents=True)
    (folder / "2611004.isdoc").write_bytes(_alza_style(_received_isdoc()))
    assert expenses.sync(tmp_path / "prijate", since_year=2025) == ["2611004"]
    e = expenses.list_expenses()[0]
    assert [(str(r.rate), str(r.base), str(r.vat)) for r in e.vat_rows] == [("21", "5000.00", "1050.00"), ("12", "100.00", "12.00")]
    assert str(e.total) == "6162.00"
    assert e.pdf_path.name == "2611004.isdoc"


def test_isdoc_next_to_pdf_wins_over_pdf_text(tmp_path: Path) -> None:
    folder = tmp_path / "prijate" / "26_11_prijate"
    folder.mkdir(parents=True)
    (folder / "2611005.pdf").write_bytes(_blank_pdf())
    (folder / "2611005.isdoc").write_bytes(_received_isdoc())
    assert expenses.sync(tmp_path / "prijate", since_year=2025) == ["2611005"]
    assert expenses.list_expenses()[0].pdf_path.name == "2611005.pdf"
    assert expenses.load_issues() == []
