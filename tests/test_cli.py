from pathlib import Path

from typer.testing import CliRunner

from ucetka.cli import app
from ucetka.isdoc.embed import extract_attachment
from ucetka.isdoc.validator import validate

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"
runner = CliRunner()


def test_validate_xml_ok() -> None:
    result = runner.invoke(
        app,
        [
            "validate-xml",
            "--invoice",
            str(EXAMPLES / "2605001.yaml"),
            "--supplier",
            str(EXAMPLES / "supplier.yaml"),
        ],
    )
    assert result.exit_code == 0, result.output
    assert "OK" in result.output


def test_issue_creates_pdf_with_attachment(tmp_path: Path) -> None:
    result = runner.invoke(
        app,
        [
            "issue",
            "--invoice",
            str(EXAMPLES / "2605001.yaml"),
            "--supplier",
            str(EXAMPLES / "supplier.yaml"),
            "--out",
            str(tmp_path),
        ],
    )
    assert result.exit_code == 0, result.output
    out_pdf = tmp_path / "faktura_2605001.pdf"
    assert out_pdf.exists()
    assert out_pdf.stat().st_size > 20_000
    extracted = extract_attachment(out_pdf.read_bytes(), "invoice.isdoc")
    validate(extracted)


def test_issue_no_qr_no_isdoc(tmp_path: Path) -> None:
    result = runner.invoke(
        app,
        [
            "issue",
            "--invoice",
            str(EXAMPLES / "2605001.yaml"),
            "--supplier",
            str(EXAMPLES / "supplier.yaml"),
            "--out",
            str(tmp_path),
            "--no-qr",
            "--no-isdoc",
        ],
    )
    assert result.exit_code == 0, result.output
    out_pdf = tmp_path / "faktura_2605001.pdf"
    assert out_pdf.exists()
    # bez QR a bez ISDOC je PDF menší a nemá attachment
    import pikepdf
    pdf = pikepdf.Pdf.open(str(out_pdf))
    assert len(pdf.attachments) == 0


def test_issue_rejects_supplier_in_invoice_yaml(tmp_path: Path) -> None:
    bad = tmp_path / "bad.yaml"
    bad.write_text((EXAMPLES / "2605001.yaml").read_text() + "\nsupplier: {name: x}\n")
    result = runner.invoke(
        app,
        [
            "issue",
            "--invoice",
            str(bad),
            "--supplier",
            str(EXAMPLES / "supplier.yaml"),
            "--out",
            str(tmp_path),
        ],
    )
    assert result.exit_code != 0


def test_init_writes_valid_profile_and_refuses_overwrite(ucetka_home, monkeypatch) -> None:
    import json

    from ucetka import ares
    from ucetka.payments import load_settings
    from ucetka.config import load_supplier

    fixture = json.loads((Path(__file__).parent / "fixtures" / "ares_87654326.json").read_text())
    def lookup(ico: str):
        if ico != "87654326":
            raise ares.CompanyNotFound(f"IČO {ico} v ARES neexistuje")
        return ares.party_from_ares(fixture)

    monkeypatch.setattr(ares, "lookup", lookup)
    (ucetka_home / "supplier.yaml").unlink()
    answers = "87654320\n87654326\ny\ny\n1234/0800\n1234567899/0800\nfaktury@example.com\n\nquarterly\ny\ny\n600000\n1574\n"
    result = CliRunner().invoke(app, ["init"], input=answers)
    assert result.exit_code == 0, result.output
    assert "87654320 v ARES neexistuje" in result.output
    assert "zkus to znovu" in result.output
    s = load_settings()
    assert (s.expense_mode, s.health_status, s.employment_income) == ("actual", "employee", 600000)
    assert load_supplier().bank_account.as_string() == "1234567899/0800"
    again = CliRunner().invoke(app, ["init"], input=answers)
    assert again.exit_code != 0 and "už existuje" in str(again.exception)
