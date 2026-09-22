"""Recorded model answers, so a demo needs no model.

The insight this implements is not an optimisation. It is the observation
that **the expensive part of the funnel is deterministic in its input**: given
the same headline and the same prompt, a model's answer is a fact about that
pair, not about the machine it ran on. Record it once, and every later run —
on a Codespace, on a laptop with no GPU, on a stage with no wifi — replays it
for free.

    laptop with a model   →  run the funnel once  →  data/reasoning/*.json
    everywhere else       →  read the recording, no model needed

WHAT IS KEYED, AND WHAT IS NOT
==============================
The key is a hash of (stage, prompt version, the exact text sent). It does
NOT include the model name. That is deliberate and it is the whole point: the
Codespace has no model, so a key naming one could never hit.

The model name IS recorded in the value, because "qwen2.5:7b said this on the
14th" and "a frontier model said this" are different claims and a planner
looking at a board is entitled to know which one they are reading.

THE PROMPT VERSION IS PART OF THE KEY
-------------------------------------
Change the system prompt and every recorded answer becomes an answer to a
different question. Hashing the prompt alongside the input means an edited
prompt misses the cache and says so, rather than silently replaying answers
to the old one — which would be the worst failure available here, because it
looks exactly like the new prompt working.

A RECORDING IS LABELLED AS ONE
------------------------------
Every hit is marked ``replayed``. The board says "recorded on the 14th by
qwen2.5:7b" rather than presenting it as a live read. This is the same
provenance discipline the source panel already applies to a fixture, and it
exists for the same reason: an answer whose age is invisible is an answer
nobody can check.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent.parent
STORE = ROOT / "data" / "reasoning"

# Set while recording. Without it the cache is read-only, which is the right
# default: a demo machine must never quietly write answers into the repo.
RECORD_ENV = "RADAR_RECORD_REASONING"

# Bump when the meaning of a stored answer changes for a reason the prompt
# hash cannot see — a new field on the Triage model, a changed abstention
# convention. Stale entries then miss rather than mislead.
SCHEMA = 1


def _key(stage: str, system: str, prompt: str) -> str:
    """Content address for one question.

    The whole (system, prompt) pair, because a prompt means nothing without
    the instructions above it, and an edited system prompt has to miss.
    """
    digest = hashlib.sha256()
    digest.update(f"{SCHEMA}\x00{stage}\x00".encode())
    digest.update(system.encode("utf-8"))
    digest.update(b"\x00")
    digest.update(prompt.encode("utf-8"))
    return digest.hexdigest()[:32]


@dataclass
class Hit:
    """A recorded answer and where it came from."""

    payload: dict[str, Any]
    model: str
    recorded_at: str
    stage: str

    @property
    def provenance(self) -> str:
        return f"recorded {self.recorded_at[:10]} by {self.model}"


@dataclass
class Store:
    """One stage's recordings, loaded lazily and written only on demand."""

    stage: str
    entries: dict[str, dict] = field(default_factory=dict)
    loaded: bool = False
    dirty: bool = False
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    @property
    def path(self) -> Path:
        return STORE / f"{self.stage}.json"

    def load(self) -> None:
        if self.loaded:
            return
        with self._lock:
            if self.loaded:
                return
            self.loaded = True
            if not self.path.exists():
                return
            try:
                data = json.loads(self.path.read_text())
            except (OSError, ValueError):
                # A corrupt recording is a missing recording, not a crash. The
                # pipeline has a live path and an unavailable path; it does not
                # need a third.
                return
            if data.get("schema") != SCHEMA:
                return
            self.entries = data.get("entries", {})

    def get(self, key: str) -> Hit | None:
        self.load()
        row = self.entries.get(key)
        if not row:
            return None
        return Hit(
            payload=row.get("payload", {}),
            model=row.get("model", "an unnamed model"),
            recorded_at=row.get("recorded_at", ""),
            stage=self.stage,
        )

    def put(self, key: str, payload: dict, model: str, at: str) -> None:
        self.load()
        with self._lock:
            self.entries[key] = {
                "payload": payload, "model": model, "recorded_at": at,
            }
            self.dirty = True

    def save(self) -> Path | None:
        if not self.dirty:
            return None
        STORE.mkdir(parents=True, exist_ok=True)
        # Sorted keys and an indent, because this file is committed and a diff
        # that reorders itself on every write is a diff nobody reviews.
        self.path.write_text(json.dumps(
            {"schema": SCHEMA, "stage": self.stage,
             "entries": dict(sorted(self.entries.items()))},
            indent=1, ensure_ascii=False, sort_keys=False,
        ) + "\n")
        self.dirty = False
        return self.path


_STORES: dict[str, Store] = {}
_STORES_LOCK = threading.Lock()


def store(stage: str) -> Store:
    with _STORES_LOCK:
        if stage not in _STORES:
            _STORES[stage] = Store(stage)
        return _STORES[stage]


def recording() -> bool:
    return os.environ.get(RECORD_ENV, "").strip().lower() in {
        "1", "true", "yes", "on"
    }


def lookup(stage: str, system: str, prompt: str) -> Hit | None:
    return store(stage).get(_key(stage, system, prompt))


def record(
    stage: str, system: str, prompt: str,
    payload: dict, model: str, at: str,
) -> None:
    """Keep an answer. No-op unless recording is switched on."""
    if not recording():
        return
    store(stage).put(_key(stage, system, prompt), payload, model, at)


def flush() -> list[Path]:
    """Write every dirty store. Called once, at the end of a recording run."""
    written = []
    for stage_store in list(_STORES.values()):
        path = stage_store.save()
        if path is not None:
            written.append(path)
    return written


def report() -> dict:
    """What is on disk, for the source panel and /api/model."""
    stages = {}
    total = 0
    models: set[str] = set()
    newest = ""
    for path in sorted(STORE.glob("*.json")) if STORE.exists() else []:
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        entries = data.get("entries", {})
        stages[path.stem] = len(entries)
        total += len(entries)
        for row in entries.values():
            if row.get("model"):
                models.add(row["model"])
            at = row.get("recorded_at", "")
            newest = max(newest, at)

    return {
        "available": total > 0,
        "entries": total,
        "stages": stages,
        "models": sorted(models),
        "recorded_at": newest,
        "note": (
            f"{total} recorded answer(s) from {', '.join(sorted(models))}, "
            f"newest {newest[:10]}. These replay without a model; each one is "
            "shown as a recording, not as a live read."
            if total else
            "No recorded answers. With no model installed the deterministic "
            "router runs alone — which is a supported state, not a degraded "
            "one."
        ),
    }
