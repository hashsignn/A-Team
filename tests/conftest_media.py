"""Small, real media files, built with the standard library.

A fixture of b"\\xff\\xd8\\xff" + 200 filler bytes is not a JPEG — it has a
marker and then noise, and a test that passes against it proves nothing about
a file a phone produced. These are structurally valid: real segments, real
chunks, real lengths, and real metadata to strip.
"""

from __future__ import annotations

import struct
import zlib


def _tiff(orientation: int = 6) -> bytes:
    """A little-endian IFD0 with orientation, make, model and a GPS pointer."""
    make, model = b"ACME PhoneCo\x00", b"Secret Model X\x00"
    count = 4
    ifd_end = 8 + 2 + count * 12 + 4
    make_off, model_off = ifd_end, ifd_end + len(make)
    gps_off = model_off + len(model)

    entries = [
        struct.pack("<HHI", 0x0112, 3, 1) + struct.pack("<HH", orientation, 0),
        struct.pack("<HHII", 0x010F, 2, len(make), make_off),
        struct.pack("<HHII", 0x0110, 2, len(model), model_off),
        struct.pack("<HHII", 0x8825, 4, 1, gps_off),
    ]
    entries.sort(key=lambda e: struct.unpack("<H", e[:2])[0])
    gps = (struct.pack("<H", 1) + struct.pack("<HHI", 0x0001, 2, 2)
           + b"N\x00\x00\x00" + struct.pack("<I", 0))
    body = struct.pack("<H", len(entries)) + b"".join(entries) + struct.pack("<I", 0)
    return b"II" + struct.pack("<HI", 42, 8) + body + make + model + gps


SCAN = bytes([0x12, 0x34, 0x56, 0x78]) * 40


def jpeg(orientation: int = 6, with_exif: bool = True) -> bytes:
    """A JPEG with EXIF (GPS, make, model, orientation) and a comment."""
    parts = [b"\xff\xd8"]
    if with_exif:
        exif = b"Exif\x00\x00" + _tiff(orientation)
        parts.append(b"\xff\xe1" + struct.pack(">H", len(exif) + 2) + exif)
        note = b"Taken at home, do not share"
        parts.append(b"\xff\xfe" + struct.pack(">H", len(note) + 2) + note)
    parts += [
        b"\xff\xdb" + struct.pack(">H", 67) + b"\x00" + bytes(range(1, 65)),
        b"\xff\xc0" + struct.pack(">H", 11) + b"\x08\x00\x10\x00\x10\x01\x01\x11\x00",
        b"\xff\xc4" + struct.pack(">H", 31) + b"\x00" + bytes(16) + bytes(range(12)),
        b"\xff\xda" + struct.pack(">H", 8) + b"\x01\x01\x00\x00\x3f\x00",
        SCAN, b"\xff\xd9",
    ]
    return b"".join(parts)


def png(with_text: bool = True) -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + kind + data
                + struct.pack(">I", zlib.crc32(kind + data)))

    ihdr = struct.pack(">IIBBBBB", 4, 4, 8, 2, 0, 0, 0)
    raw = b"".join(b"\x00" + b"\xff\x00\x00" * 4 for _ in range(4))
    out = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
    if with_text:
        out += chunk(b"tEXt", b"Author\x00Jane Driver")
        out += chunk(b"eXIf", b"II" + struct.pack("<HI", 42, 8) + b"\x00" * 16)
    return out + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"")


def webp(with_exif: bool = True) -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        return kind + struct.pack("<I", len(data)) + data + (b"\x00" if len(data) & 1 else b"")

    body = b"WEBP" + chunk(b"VP8L", b"\x2f" + b"\x00" * 20)
    if with_exif:
        body += chunk(b"EXIF", b"II*\x00" + b"\x00" * 30)
    # The size counts everything after it, INCLUDING the WEBP signature.
    return b"RIFF" + struct.pack("<I", len(body)) + body
