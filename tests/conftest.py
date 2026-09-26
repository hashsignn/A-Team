"""Every test reads the samples it was written against, never a recording.

data/fixtures is what the demo replays: the samples, until somebody records
the real sources over them — which is the whole point of recording. The tests
assert things about particular headlines and a particular river: the Hormuz
announcement, a blockade at Antwerp, a Rhine falling towards its derate
bands. Against a recording of an ordinary week those assertions fail for no
reason but the world having been quiet.

So the tests read their own copy of the samples, in tests/fixtures, and a
recording committed to data/fixtures changes nothing a test sees. Recorded
model answers are kept out the same way: every run starts with none, exactly
as it did before anything was recorded.

And so is the customer's own data. config/flows.yaml — Sika's order history,
gitignored — reweights which lanes the synthetic book is drawn from, so on
the one laptop that holds it the book has fewer barges and a different Gulf,
and tests written about the shipped book fail there and nowhere else. The
tests of the flow file itself pass their own paths and are unaffected.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from engine.ingest import flows, observations
from engine.reason import cache

SAMPLES = Path(__file__).resolve().parent / "fixtures"


@pytest.fixture(autouse=True, scope="session")
def _samples_not_recordings(tmp_path_factory):
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(observations, "FIXTURE_DIR", SAMPLES)
        patch.setattr(cache, "STORE", tmp_path_factory.mktemp("reasoning"))
        patch.setattr(flows, "FLOWS_PATH", tmp_path_factory.mktemp("no-customer") / "flows.yaml")
        cache._STORES.clear()
        yield
    cache._STORES.clear()
