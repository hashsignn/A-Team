"""Strip metadata from a photo without touching a single pixel.

WHY NOT RE-ENCODE
=================
The usual advice is "open it with an imaging library and save it again",
which drops the metadata as a side effect. That is the wrong tool here for
two reasons.

**These photos are evidence.** A driver photographs a shifted pallet and a
planner acts on it; later somebody may need to show that the picture is what
the camera produced. Re-encoding a JPEG recompresses it — the bytes change,
the artefacts change, and the file is no longer the one that was taken. This
module rewrites the CONTAINER and copies the image data through untouched, so
the compressed scan is bit-for-bit what the phone wrote.

**It would cost a dependency.** The base install is seven packages precisely
so the thing can be run by somebody who was handed a laptop. Pillow is a
large native dependency to add for a job that is, in all three formats we
accept, walking a list of chunks and not copying some of them.

WHAT GOES
=========
Everything that is not image data:

* **JPEG** — every APP segment. APP1 is where EXIF lives (GPS, camera serial,
  the owner's name on some phones) and where XMP lives; APP2 carries ICC and
  sometimes MPF; APP13 carries Photoshop IRB, which carries IPTC. Comments
  (COM) go too: cameras and apps write surprising things there.
* **PNG** — eXIf, tEXt, iTXt, zTXt and tIME. The text chunks are where
  editors leave software names, copyright lines and free-form comments.
* **WebP** — EXIF, XMP and ICCP chunks.

WHAT IS KEPT, AND WHY IT IS THE HARD PART
=========================================
The EXIF **orientation** tag. Phones very often store the sensor's raw
landscape pixels and record "rotate this 90°" alongside; strip that tag
naively and a whole class of photos display on their side. So the tag is READ
before the metadata is discarded and returned to the caller, which stores it
beside the file and hands it to the page — the rotation survives as a number
we control rather than as a block of camera metadata we cannot audit.

That is the difference between "we stripped EXIF" and "we stripped EXIF
without breaking the photos".
"""

from __future__ import annotations

import struct

# EXIF orientation values. 1 is upright; the rest are rotations and mirrors.
UPRIGHT = 1

# JPEG markers that carry no length field and no payload.
_STANDALONE = {0xD8, 0xD9, 0x01, *range(0xD0, 0xD8)}
# Segments we drop: APP0..APP15 and COM. APP0 is JFIF, which is harmless but
# also unnecessary — a decoder does not need it, and dropping the whole
# APP range is a rule with no exceptions to get wrong.
_DROP = {*range(0xE0, 0xF0), 0xFE}

_PNG_DROP = {b"eXIf", b"tEXt", b"iTXt", b"zTXt", b"tIME"}
_WEBP_DROP = {b"EXIF", b"XMP ", b"ICCP"}


def strip(data: bytes) -> tuple[bytes, int]:
    """Return (clean bytes, orientation).

    Orientation is 1 when the photo carried none, which is also what "no
    rotation needed" means — so a caller that ignores the distinction still
    behaves correctly.

    Never raises on malformed input: it returns the original bytes with
    orientation 1. A photo that cannot be parsed is not a photo we should be
    guessing about, and the caller's own type check has already decided
    whether to accept it at all.
    """
    try:
        if data.startswith(b"\xff\xd8\xff"):
            return _strip_jpeg(data)
        if data.startswith(b"\x89PNG\r\n\x1a\n"):
            return _strip_png(data), UPRIGHT
        if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
            return _strip_webp(data), UPRIGHT
    except (struct.error, IndexError, ValueError):
        return data, UPRIGHT
    return data, UPRIGHT


# ---------------------------------------------------------------------
# JPEG
# ---------------------------------------------------------------------
def _strip_jpeg(data: bytes) -> tuple[bytes, int]:
    out = bytearray(b"\xff\xd8")
    orientation = UPRIGHT
    i = 2

    while i < len(data) - 1:
        if data[i] != 0xFF:
            # Desynchronised. Copy the remainder verbatim rather than
            # guessing: a half-parsed JPEG written back out is a corrupt file.
            out += data[i:]
            break

        marker = data[i + 1]
        if marker == 0xFF:          # fill byte, legal between segments
            i += 1
            continue
        if marker in _STANDALONE:
            if marker != 0xD8:
                out += data[i:i + 2]
            i += 2
            continue

        length = struct.unpack(">H", data[i + 2:i + 4])[0]
        segment = data[i:i + 2 + length]

        if marker in _DROP:
            if marker == 0xE1 and segment[4:10] == b"Exif\x00\x00":
                orientation = _orientation_from_exif(segment[10:]) or orientation
            # and not copied
        else:
            out += segment

        i += 2 + length

        if marker == 0xDA:          # start of scan: entropy data to the end
            out += data[i:]
            break

    return bytes(out), orientation


def _orientation_from_exif(tiff: bytes) -> int | None:
    """Read tag 0x0112 out of IFD0. Nothing else is read, on purpose.

    A full EXIF parser is a liability: it is a lot of surface for reading
    attacker-controlled bytes, and everything else in there is exactly what
    we are removing. This walks one directory looking for one tag.
    """
    if len(tiff) < 8:
        return None
    if tiff[:2] == b"II":
        endian = "<"
    elif tiff[:2] == b"MM":
        endian = ">"
    else:
        return None

    (offset,) = struct.unpack(endian + "I", tiff[4:8])
    if offset + 2 > len(tiff):
        return None
    (count,) = struct.unpack(endian + "H", tiff[offset:offset + 2])

    for index in range(min(count, 512)):        # bounded: a claimed count is not a fact
        entry = offset + 2 + index * 12
        if entry + 12 > len(tiff):
            return None
        tag, kind = struct.unpack(endian + "HH", tiff[entry:entry + 4])
        if tag != 0x0112:
            continue
        if kind != 3:                            # SHORT
            return None
        (value,) = struct.unpack(endian + "H", tiff[entry + 8:entry + 10])
        return value if 1 <= value <= 8 else None
    return None


# ---------------------------------------------------------------------
# PNG
# ---------------------------------------------------------------------
def _strip_png(data: bytes) -> bytes:
    out = bytearray(data[:8])
    i = 8
    while i + 8 <= len(data):
        (length,) = struct.unpack(">I", data[i:i + 4])
        kind = data[i + 4:i + 8]
        end = i + 12 + length
        if end > len(data):
            out += data[i:]
            break
        if kind not in _PNG_DROP:
            out += data[i:end]
        i = end
        if kind == b"IEND":
            break
    return bytes(out)


# ---------------------------------------------------------------------
# WebP
# ---------------------------------------------------------------------
def _strip_webp(data: bytes) -> bytes:
    body = bytearray(b"WEBP")
    i = 12
    while i + 8 <= len(data):
        kind = data[i:i + 4]
        (size,) = struct.unpack("<I", data[i + 4:i + 8])
        padded = size + (size & 1)               # chunks are padded to even
        end = i + 8 + padded
        if end > len(data):
            break
        if kind not in _WEBP_DROP:
            body += data[i:end]
        i = end

    # The RIFF size field counts everything after it — including the WEBP
    # signature, which is part of the payload and not part of the header.
    # Slicing it off here produced a file whose first chunk sat where the
    # signature should be; nothing downstream recognised it as a WebP, which
    # is a corrupt photo rather than a stripped one.
    return b"RIFF" + struct.pack("<I", len(body)) + bytes(body)
