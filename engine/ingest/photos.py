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
import json
import re
from pathlib import Path

from engine.ingest.exif import UPRIGHT, strip

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
    """Strip the metadata, then write. Returns id, type, size and orientation.

    STRIPPED BEFORE ANYTHING ELSE HAPPENS TO IT. A phone photo carries the
    GPS of whoever took it, the camera's serial number, and on some devices
    the owner's name. That is the photographer's location, which is not
    always the freight's and is never ours to publish — and once it is on
    disk, "we will strip it later" is a promise nobody keeps.

    The id is the hash of the CLEAN bytes, so it identifies what we actually
    hold. Hashing the original would mean the id described a file that no
    longer exists anywhere.

    Still content-addressed: stripping is deterministic, so the same photo
    sent twice still lands once. A driver whose connection dropped mid-upload
    and retried has not doubled anything.
    """
    if len(data) > MAX_BYTES:
        raise PhotoError(f"photo is larger than {MAX_BYTES // (1024 * 1024)} MB")

    # Sniffed on the ORIGINAL: deciding what it is, before rewriting it, is
    # the only order that makes sense.
    ext, media = sniff(data)
    clean, orientation = strip(data)

    photo_id = hashlib.sha256(clean).hexdigest()[:32]
    folder = directory or DEFAULT_DIR
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{photo_id}.{ext}"
    if not path.exists():
        path.write_bytes(clean)

    # The rotation, kept as a number we control rather than as a block of
    # camera metadata we cannot audit. Written only when it is not the
    # default, so the common case leaves no extra file.
    if orientation != UPRIGHT:
        (folder / f"{photo_id}.json").write_text(
            json.dumps({"orientation": orientation})
        )

    return {
        "photo_id": photo_id,
        "media_type": media,
        "bytes": len(clean),
        "stripped_bytes": len(data) - len(clean),
        "orientation": orientation,
    }


def orientation_for(photo_id: str, directory: Path | None = None) -> int:
    """How the page should rotate this photo. 1 means leave it alone."""
    if not ID_RE.match(photo_id or ""):
        return UPRIGHT
    sidecar = (directory or DEFAULT_DIR) / f"{photo_id}.json"
    if not sidecar.exists():
        return UPRIGHT
    try:
        value = int(json.loads(sidecar.read_text()).get("orientation", UPRIGHT))
    except (OSError, ValueError, TypeError):
        return UPRIGHT
    return value if 1 <= value <= 8 else UPRIGHT


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
