"""Koncept e-mailu s fakturou v Mail.app. Nikdy neodesílá — odeslání je vždy na uživateli."""
from __future__ import annotations

import subprocess
from pathlib import Path

# Hodnoty jdou přes argv, ne interpolací do skriptu — žádné escapování uvozovek v textu e-mailu.
_SCRIPT = """
on run argv
    set {toAddr, subj, body, attachPath} to argv
    tell application "Mail"
        set msg to make new outgoing message with properties {subject:subj, content:body & return & return, visible:true}
        tell msg
            if toAddr is not "" then make new to recipient at end of to recipients with properties {address:toAddr}
            make new attachment with properties {file name:(POSIX file attachPath)} at after the last paragraph
        end tell
        activate
    end tell
end run
"""


def osascript_args(to: str | None, subject: str, body: str, attachment: Path) -> list[str]:
    return ["osascript", "-e", _SCRIPT, to or "", subject, body, str(attachment.resolve())]


def create_draft(to: str | None, subject: str, body: str, attachment: Path) -> None:
    if not attachment.exists():
        raise FileNotFoundError(attachment)
    subprocess.run(osascript_args(to, subject, body, attachment), check=True, capture_output=True, text=True)
