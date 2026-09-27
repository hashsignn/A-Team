"""Files a customer's machine wrote are read whatever encoding Windows used.

A laptop's config/flows.yaml had been written by an older import script in
Windows-1252, so the em dash in its header was byte 0x97 — and every run on
that laptop stopped on "'utf-8' codec can't decode byte 0x97" before reading
a flow, including the one-off run that records the model's answers.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from engine import textfiles
from engine.config import EXAMPLE_DIR, load_config
from engine.ingest import flows

FLOW_FILE = """\
# Sika intercompany flows — DERIVED FROM THE CUSTOMER'S EXPORT
flows:
  - lane: DE_US
    origin_country: DE
    destination_country: US
    documents: 12
    monthly: {2025-01: 12}
"""


def test_an_old_windows_flow_file_is_read_and_flagged(tmp_path):
    path = tmp_path / "flows.yaml"
    path.write_bytes(FLOW_FILE.encode("cp1252"))
    assert b"\x97" in path.read_bytes()           # the em dash, as Windows wrote it
    got = flows.load(path)
    assert got.available and got.by_pair == {("DE", "US"): 12}
    assert "cp1252" in got.detail and "import_sika_flows.py" in got.detail


def test_a_notepad_byte_order_mark_is_not_content(tmp_path):
    path = tmp_path / "flows.yaml"
    path.write_bytes(FLOW_FILE.encode("utf-8-sig"))
    got = flows.load(path)
    assert got.available and got.by_pair == {("DE", "US"): 12}
    assert "cp1252" not in got.detail


def test_a_file_nothing_can_read_is_unreadable_not_a_crash(tmp_path):
    path = tmp_path / "flows.yaml"
    path.write_bytes(b"flows:\n  - lane: \x81\x8d\x8f\x90\x9d\xff\xfe\n")
    got = flows.load(path)
    assert not got.available and "unreadable" in got.detail


def test_a_customer_overlay_saved_on_windows_still_loads(tmp_path):
    customer = tmp_path / "config"
    customer.mkdir()
    scoring = (EXAMPLE_DIR / "scoring.yaml").read_text(encoding="utf-8")
    (customer / "scoring.yaml").write_bytes(
        ("# edited on the planner's laptop — Windows-1252\n" + scoring).encode("cp1252",
                                                                             errors="replace"))
    config = load_config(customer_dir=customer)
    assert not config.is_example("scoring")
    assert config.scoring


def test_utf8_is_always_tried_first():
    text, encoding = textfiles.read_customer_text(Path(shutil.__file__))
    assert encoding == "utf-8-sig" and "def copyfile" in text
