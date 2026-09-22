"""Photos from the field: stored as files, referenced by id.

WHY NOT IN THE LOG
==================
The report log is append-only JSON Lines, and it is evidence. It has to stay
something an auditor can open with `cat` in five years without our help.
Base64 images inline would make one line of it megabytes long and the whole
file unreadable — so a report carries photo IDS, and the bytes live beside it.

WHAT IS ACCEPTED
================
JPEG, PNG and WebP, identified by their MAGIC BYTES rather than by the
filename or the Content-Type header, both of which the sender chooses. A file
claiming to be a JPEG and starting with ``<?php`` is not a photo, and the
check that catches it costs four bytes of comparison.

The id is the SHA-256 of the content, so the same photo sent twice is stored
once — a driver retrying on a bad connection does not fill the disk — and the
id cannot be forged into a path: it is hex, and the store refuses anything
that is not.

THE PROTOTYPE'S LIMIT
=====================
This endpoint is unauthenticated, like the reports it accompanies. Bounded
size, bounded count, content-sniffed type and a content-addressed name are
what make that survivable for a demo; they are not what makes it safe for
real drivers. See SECURITY.md.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_DIR = ROOT / "data" / "photos"

MAX_BYTES = 6 * 1024 * 1024        # a phone photo, not a video
ID_RE = re.compile(r"^[a-f0-9]{32}$")

# (magic prefix, extension, media type). Order matters only for readability.
KINDS: tuple[tuple[bytes, str, str], ...] = (
    (b"\xff\xd8\xff", "jpg", "image/jpeg"),
    (b"\x89PNG\r\n\x1a\n", "png", "image/png"),
    (b"RIFF", "webp", "image/webp"),      # RIFF....WEBP, checked below
)


class PhotoError(ValueError):
    """The upload cannot be stored. Says why, in words a person can act on."""


def sniff(data: bytes) -> tuple[str, str]:
    """(extension, media type) from the CONTENT. Raises if it is not a photo."""
    if not data:
        raise PhotoError("empty upload")
    for magic, ext, media in KINDS:
        if not data.startswith(magic):
            continue
        if ext == "webp" and data[8:12] != b"WEBP":
            continue
        return ext, media
    raise PhotoError(
        "not a JPEG, PNG or WebP — checked by content, not by file name"
    )


def store(data: bytes, directory: Path | None = None) -> dict:
    """Write one photo. Returns its id, type and size.

    Content-addressed, so the same bytes stored twice produce one file and
    the same id. A driver whose connection dropped mid-upload and retried has
    not doubled anything.
    """
    if len(data) > MAX_BYTES:
        raise PhotoError(f"photo is larger than {MAX_BYTES // (1024 * 1024)} MB")
    ext, media = sniff(data)

    photo_id = hashlib.sha256(data).hexdigest()[:32]
    folder = directory or DEFAULT_DIR
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{photo_id}.{ext}"
    if not path.exists():
        path.write_bytes(data)
    return {"photo_id": photo_id, "media_type": media, "bytes": len(data)}


def path_for(photo_id: str, directory: Path | None = None) -> Path | None:
    """Where a photo lives, or None.

    The id is validated against a hex pattern BEFORE it touches the
    filesystem. It arrives from a URL, and an id is the one place a caller
    could otherwise ask for '../../.env'.
    """
    if not ID_RE.match(photo_id or ""):
        return None
    folder = directory or DEFAULT_DIR
    for _, ext, _ in KINDS:
        candidate = folder / f"{photo_id}.{ext}"
        if candidate.exists():
            return candidate
    return None


def media_type_for(path: Path) -> str:
    for _, ext, media in KINDS:
        if path.suffix == f".{ext}":
            return media
    return "application/octet-stream"
