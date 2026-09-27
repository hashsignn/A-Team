"""Reading a text file somebody else wrote, whatever Windows did to it.

Everything this repository writes is UTF-8, and says so. The files a
customer's machine writes are not always: an older import script on Windows
saved config/flows.yaml in the machine's legacy encoding (Windows-1252), so
the em dash in its header came out as byte 0x97, and the next run stopped
dead on "'utf-8' codec can't decode byte 0x97" before it read a single flow.
Notepad adds a byte-order mark to UTF-8, which YAML then reads as content.

So a file under config/ is read as UTF-8 (with or without the mark), then as
Windows-1252, and the caller is told which — a file read the second way is
worth rewriting, and the board says so rather than guessing silently.
"""

from __future__ import annotations

from pathlib import Path

# Tried in order. cp1252 decodes almost any byte string, so it is last: it is
# the answer for a file that is not UTF-8, never a reason to skip UTF-8.
ENCODINGS = ("utf-8-sig", "cp1252")


def read_customer_text(path: Path) -> tuple[str, str]:
    """(text, the encoding it was read with). Raises UnicodeDecodeError only
    if no encoding in ENCODINGS can read it."""
    raw = path.read_bytes()
    last: UnicodeDecodeError | None = None
    for encoding in ENCODINGS:
        try:
            return raw.decode(encoding), encoding
        except UnicodeDecodeError as exc:
            last = exc
    raise last  # type: ignore[misc]  # ENCODINGS is never empty


def legacy(encoding: str) -> bool:
    """Whether a file was read in something other than UTF-8."""
    return encoding not in ("utf-8", "utf-8-sig")
