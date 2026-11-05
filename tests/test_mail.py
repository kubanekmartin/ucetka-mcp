from pathlib import Path

import pytest

from ucetka import mail


def test_osascript_never_sends_and_passes_values_as_argv(tmp_path: Path) -> None:
    pdf = tmp_path / "f.pdf"
    pdf.write_bytes(b"%PDF")
    args = mail.osascript_args("a@b.cz", 'Faktura "1"', "Text", pdf)
    script = args[2]
    assert "send" not in script.split()
    assert "visible:true" in script
    assert args[3:] == ["a@b.cz", 'Faktura "1"', "Text", str(pdf.resolve())]


def test_missing_attachment_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        mail.create_draft("a@b.cz", "s", "b", tmp_path / "missing.pdf")
