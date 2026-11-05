from pathlib import Path

import pytest
import yaml

from ucetka.customers import CustomerNotFound, get_customer, load_customers
from ucetka.models import Party

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"


def test_load_customers_from_yaml() -> None:
    book = load_customers(EXAMPLES / "customers.yaml")
    assert "vzorova-firma" in book
    assert isinstance(book["vzorova-firma"], Party)
    assert book["vzorova-firma"].ico == "87654326"


def test_get_customer_returns_party() -> None:
    cust = get_customer("vzorova-firma", path=EXAMPLES / "customers.yaml")
    assert cust.name == "Vzorová firma s.r.o."


def test_missing_customer_raises() -> None:
    with pytest.raises(CustomerNotFound):
        get_customer("does-not-exist", path=EXAMPLES / "customers.yaml")


def test_customer_id_lookup_via_cli_works(tmp_path: Path) -> None:
    from typer.testing import CliRunner

    from ucetka.cli import app

    runner = CliRunner()
    # vyrobíme YAML co používá customer_id místo inline customer
    inv_yaml = yaml.safe_load((EXAMPLES / "2605001.yaml").read_text())
    inv_yaml.pop("customer")
    inv_yaml["customer_id"] = "vzorova-firma"
    inv_path = tmp_path / "inv.yaml"
    inv_path.write_text(yaml.safe_dump(inv_yaml, allow_unicode=True))

    result = runner.invoke(
        app,
        [
            "issue",
            "--invoice", str(inv_path),
            "--supplier", str(EXAMPLES / "supplier.yaml"),
            "--customers", str(EXAMPLES / "customers.yaml"),
            "--out", str(tmp_path),
        ],
    )
    assert result.exit_code == 0, result.output
    assert (tmp_path / "faktura_2605001.pdf").exists()
