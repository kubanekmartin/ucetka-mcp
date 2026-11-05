import shutil
from pathlib import Path

import pytest

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"


@pytest.fixture(autouse=True)
def ucetka_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "ucetka-home"
    home.mkdir()
    shutil.copy(EXAMPLES / "supplier.yaml", home / "supplier.yaml")
    shutil.copy(EXAMPLES / "customers.yaml", home / "customers.yaml")
    monkeypatch.setenv("UCETKA_HOME", str(home))
    monkeypatch.setenv("UCETKA_OUT", str(tmp_path / "out"))
    monkeypatch.setenv("UCETKA_RECEIVED", str(tmp_path / "received"))
    monkeypatch.setenv("UCETKA_REPORTS", str(tmp_path / "reports"))
    return home
