"""Photos from the field: stored as files, identified by content.

The report log is append-only JSON Lines and it is evidence — it has to stay
something an auditor can open with `cat` in five years. Base64 images inline
would make one line of it megabytes long, so a report carries photo IDS and
the bytes live beside it.
"""

from __future__ import annotations

import pytest

from engine.ingest import photos as P
from tests.conftest_media import SCAN, jpeg, png, webp

# Structurally real files, not a marker followed by filler. A fixture of
# b"\xff\xd8\xff" + 200 bytes of noise is not a JPEG, and a test that passes
# against it proves nothing about a file a phone produced.
JPEG, PNG, WEBP = jpeg(), png(), webp()


@pytest.fixture
def store(tmp_path):
    return tmp_path / "photos"


@pytest.mark.parametrize(("data", "media"), [
    (JPEG, "image/jpeg"), (PNG, "image/png"), (WEBP, "image/webp"),
])
def test_the_three_types_are_accepted(data, media, store):
    assert P.store(data, store)["media_type"] == media


def test_the_type_comes_from_the_content_not_the_name(store):
    """A file claiming to be a JPEG and starting with <?php is not a photo,
    and the filename and Content-Type are both chosen by the sender."""
    with pytest.raises(P.PhotoError, match="checked by content"):
        P.store(b"<?php system($_GET['c']); ?>", store)


@pytest.mark.parametrize("data", [b"", b"GIF89a" + b"x" * 50, b"\xff\xd8", b"RIFFxxxxNOPE"])
def test_anything_else_is_refused(data, store):
    with pytest.raises(P.PhotoError):
        P.store(data, store)


def test_oversize_is_refused(store):
    with pytest.raises(P.PhotoError, match="larger than"):
        P.store(b"\xff\xd8\xff" + b"x" * (P.MAX_BYTES + 1), store)


def test_the_same_photo_twice_is_stored_once(store):
    """A driver whose connection dropped mid-upload and retried has not
    doubled anything."""
    first, second = P.store(JPEG, store), P.store(JPEG, store)
    assert first["photo_id"] == second["photo_id"]
    # One IMAGE. There is also an orientation sidecar, which is not a second
    # copy of the photo — it is the rotation that stripping the EXIF removed.
    assert len(list(store.glob("*.jpg"))) == 1
    assert len(list(store.glob("*.json"))) == 1


def test_different_photos_get_different_ids(store):
    assert P.store(JPEG, store)["photo_id"] != P.store(PNG, store)["photo_id"]


@pytest.mark.parametrize("nasty", [
    "../../../etc/passwd", "..%2f..%2fetc", "a" * 100, "", "NOTHEX!!",
    "/etc/passwd", "abc.jpg",
])
def test_a_path_cannot_be_smuggled_through_an_id(nasty, store):
    """The id arrives from a URL. It is pattern-checked BEFORE it touches the
    filesystem, because it is the one place a caller could ask for '../../.env'."""
    assert P.path_for(nasty, store) is None


def test_a_real_id_resolves(store):
    """The bytes on disk are the STRIPPED ones, not the upload.

    This asserted byte-equality with the input, which stripping correctly
    breaks. The property that matters is that the image data survived — the
    metadata is supposed to be gone.
    """
    stored = P.store(JPEG, store)
    path = P.path_for(stored["photo_id"], store)
    assert path is not None
    on_disk = path.read_bytes()
    assert SCAN in on_disk, "the compressed scan must survive untouched"
    assert on_disk.startswith(b"\xff\xd8") and on_disk.endswith(b"\xff\xd9")


def test_an_unknown_but_well_formed_id_is_simply_absent(store):
    assert P.path_for("0" * 32, store) is None


def test_ids_are_hex_and_fixed_length(store):
    photo_id = P.store(JPEG, store)["photo_id"]
    assert P.ID_RE.match(photo_id), photo_id
