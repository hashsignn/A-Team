"""Metadata stripping, proved against structurally real files.

The claim under test is narrow and has to be exact: everything that is not
image data goes, the image data itself is untouched, and the one tag we
cannot afford to lose comes back as a number.
"""

from __future__ import annotations

import struct

import pytest

from engine.ingest.exif import UPRIGHT, strip
from tests.conftest_media import SCAN, jpeg, png, webp

# Every string a phone or an editor might leave behind in these fixtures.
LEAKS = (
    b"Exif", b"ACME PhoneCo", b"Secret Model X", b"Taken at home, do not share",
    b"Jane Driver", b"eXIf", b"tEXt",
)


# =====================================================================
# Nothing personal survives
# =====================================================================
@pytest.mark.parametrize("builder", [jpeg, png, webp])
def test_no_metadata_survives(builder):
    clean, _ = strip(builder())
    found = [probe.decode(errors="replace") for probe in LEAKS if probe in clean]
    assert not found, f"still in the file: {found}"


def test_the_gps_pointer_is_gone():
    """The tag that matters most. A phone photo carries the GPS of whoever
    took it, which is the photographer's location — not always the freight's,
    and never ours to publish."""
    clean, _ = strip(jpeg())
    assert b"\x25\x88" not in clean and b"\x88\x25" not in clean


def test_something_was_actually_removed():
    """A stripper that returns its input unchanged passes every 'is it gone'
    test ever written."""
    raw = jpeg()
    clean, _ = strip(raw)
    assert len(clean) < len(raw)


# =====================================================================
# The image itself is untouched
# =====================================================================
def test_the_compressed_scan_is_byte_for_byte_identical():
    """These photos are evidence. Re-encoding a JPEG changes the bytes and the
    artefacts, and the file stops being the one the camera produced — so the
    container is rewritten and the scan is copied through."""
    clean, _ = strip(jpeg())
    assert SCAN in clean


@pytest.mark.parametrize(("builder", "head", "keep"), [
    (jpeg, b"\xff\xd8", b"\xff\xda"),
    (png, b"\x89PNG\r\n\x1a\n", b"IDAT"),
    (webp, b"RIFF", b"VP8L"),
])
def test_the_file_is_still_that_kind_of_file(builder, head, keep):
    clean, _ = strip(builder())
    assert clean.startswith(head)
    assert keep in clean, "the image data chunk was dropped with the metadata"


def test_the_webp_size_field_is_corrected():
    """RIFF declares its own length. Drop a chunk without fixing it and every
    decoder downstream reads past the end of the file."""
    clean, _ = strip(webp())
    assert struct.unpack("<I", clean[4:8])[0] == len(clean) - 8
    assert clean[8:12] == b"WEBP", "the signature is payload, not header"


def test_png_chunks_stay_intact():
    clean, _ = strip(png())
    assert clean.endswith(b"IEND" + struct.pack(">I", 0xAE426082))


# =====================================================================
# Orientation: the hard part
# =====================================================================
def test_the_orientation_is_read_before_it_is_destroyed():
    """Phones store landscape pixels and a 'rotate 90°' tag. Strip that
    naively and a whole class of photos display on their side."""
    _, orientation = strip(jpeg(orientation=6))
    assert orientation == 6


@pytest.mark.parametrize("value", [1, 2, 3, 4, 5, 6, 7, 8])
def test_every_legal_orientation_round_trips(value):
    _, orientation = strip(jpeg(orientation=value))
    assert orientation == value


def test_a_nonsense_orientation_is_ignored():
    """A value outside 1-8 is not a rotation, it is a corrupt or hostile tag,
    and applying it would transform the photo by an undefined amount."""
    _, orientation = strip(jpeg(orientation=99))
    assert orientation == UPRIGHT


def test_no_exif_means_upright():
    """Which is also what 'no rotation needed' means, so a caller that ignores
    the distinction still behaves correctly."""
    _, orientation = strip(jpeg(with_exif=False))
    assert orientation == UPRIGHT


# =====================================================================
# Malformed input is returned, not mangled
# =====================================================================
@pytest.mark.parametrize("data", [
    b"", b"\xff\xd8", b"\xff\xd8\xff", b"not an image at all",
    b"\x89PNG\r\n\x1a\n", b"RIFF\x00\x00\x00\x00WEBP",
    b"\xff\xd8\xff\xe1\xff\xff" + b"\x00" * 4,      # a length that overruns
])
def test_garbage_never_raises(data):
    """A photo that cannot be parsed is not one we should be guessing about.
    The caller's own type check has already decided whether to accept it."""
    clean, orientation = strip(data)
    assert isinstance(clean, bytes)
    assert orientation == UPRIGHT


def test_a_truncated_exif_does_not_crash_the_parser():
    raw = jpeg()
    assert strip(raw[: len(raw) // 2])[1] in range(1, 9)
