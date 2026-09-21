"""The model layer: local first, API optional, neither required.

WHY LOCAL FIRST
===============
Three reasons, in order of how much they matter here:

1. **Freight data is commercially sensitive.** A planner's order book, its
   customers and its penalties are exactly the things a company will not post
   to a third party to try a demo. Running on the planner's own machine
   removes the conversation entirely.
2. **The demo has to work with the cable pulled out.** This environment's
   egress proxy blocks every external data host already; a pipeline that needs
   a remote model is a pipeline that cannot be shown.
3. **It is free.** A hindcast over two years of archived feeds is thousands of
   calls. At local-model prices that is an afternoon; at API prices it is a
   budget conversation.

The API path exists because a frontier model is genuinely better on the hard
judgement calls — reading "unless talks resume" as a conditional, spotting a
second-order effect. It is opt-in, never required.

THE BACKEND IS A COMPONENT, NOT THE ARCHITECTURE
------------------------------------------------
Every caller asks for ``parse()`` and gets back a validated Pydantic model, or
an ``Abstention``. Nothing downstream knows or cares which engine produced it,
and nothing downstream changes when you switch. That is the property that lets
the rules challenger run in the same slot (see engine/variables/rules.py).

NO MODEL IS A SUPPORTED STATE, NOT A DEGRADED ONE
-------------------------------------------------
With no backend reachable, ``available()`` is False and the pipeline runs the
deterministic router alone. The board is complete; it just has no reasoned
layer to measure against, and the UI says so rather than pretending.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from enum import Enum
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

log = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)

# ---------------------------------------------------------------------
# Configuration, all overridable by environment
# ---------------------------------------------------------------------
OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://localhost:11434")

# qwen2.5 at 7B is the recommendation for this workload, and the reasoning is
# in README.md rather than here: it follows a JSON schema without a grammar,
# it is multilingual (Rhine and port notices are German and Dutch), and 7B fits
# in the ~5 GB a planner's laptop can spare beside everything else they run.
LOCAL_MODEL = os.environ.get("RADAR_LOCAL_MODEL", "qwen2.5:7b-instruct")

# The API path, if a key is present. Opus 5 is the default because this is the
# judgement-heavy end of the pipeline, not the bulk end.
API_MODEL = os.environ.get("RADAR_API_MODEL", "claude-opus-5")

TIMEOUT_S = float(os.environ.get("RADAR_LLM_TIMEOUT", "60"))


class Backend(str, Enum):
    LOCAL = "local"
    API = "api"
    NONE = "none"


@dataclass(frozen=True)
class BackendStatus:
    """What is actually running, and what it would take to change that.

    Shaped like a FeedReport on purpose: the model is an input like any other,
    and "absent, and here is what connecting it unlocks" is the same sentence
    the source panel already makes about a missing gauge.
    """

    backend: Backend
    model: str | None
    detail: str
    unlocks_if_connected: str

    @property
    def available(self) -> bool:
        return self.backend is not Backend.NONE


def _ollama_reachable() -> bool:
    try:
        with urllib.request.urlopen(f"{OLLAMA_HOST}/api/tags", timeout=2) as response:
            return response.status == 200
    except (urllib.error.URLError, OSError, TimeoutError):
        return False


def _has_api_key() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY"))


def detect(override: str | None = None) -> BackendStatus:
    """Which engine will run.

    Local wins when both are available: the data stays on the machine unless
    the operator deliberately says otherwise, which is the right default for a
    customer's order book.
    """
    choice = (override or os.environ.get("RADAR_LLM_BACKEND") or "").strip().lower()

    if choice == Backend.NONE.value:
        return BackendStatus(
            Backend.NONE, None,
            "disabled by RADAR_LLM_BACKEND=none",
            "The deterministic router is running alone.",
        )

    if choice in ("", Backend.LOCAL.value) and _ollama_reachable():
        return BackendStatus(
            Backend.LOCAL, LOCAL_MODEL,
            f"Ollama at {OLLAMA_HOST}, model {LOCAL_MODEL}",
            "",
        )

    if choice in ("", Backend.API.value) and _has_api_key():
        return BackendStatus(
            Backend.API, API_MODEL,
            f"Anthropic API, model {API_MODEL}",
            "",
        )

    if choice == Backend.LOCAL.value:
        detail = f"Ollama not reachable at {OLLAMA_HOST}"
    elif choice == Backend.API.value:
        detail = "ANTHROPIC_API_KEY is not set"
    else:
        detail = f"no Ollama at {OLLAMA_HOST}, and ANTHROPIC_API_KEY is not set"

    return BackendStatus(
        Backend.NONE, None, detail,
        "Reading the conditional in “unless talks resume”, the duration in "
        "“at least ten days”, and second-order effects the keyword router "
        "cannot see. The router still produces a complete board without it.",
    )


# ---------------------------------------------------------------------
# Calling
# ---------------------------------------------------------------------
def _call_local(system: str, prompt: str, schema: dict) -> str:
    """Ollama's /api/chat with structured output.

    Plain urllib: this is one POST to localhost, and a dependency for that is
    a dependency the planner has to install to run the demo.
    """
    body = json.dumps({
        "model": LOCAL_MODEL,
        "stream": False,
        "format": schema,
        "options": {"temperature": 0},
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": prompt},
        ],
    }).encode()
    request = urllib.request.Request(
        f"{OLLAMA_HOST}/api/chat", data=body,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=TIMEOUT_S) as response:
        payload = json.loads(response.read())
    return payload["message"]["content"]


def _call_api(system: str, prompt: str, schema: dict) -> str:
    """The Anthropic SDK, imported lazily.

    Lazily because `anthropic` is in requirements-optional.txt: the base
    install is seven packages and adding an SDK nobody on the local path uses
    would undo that.
    """
    import anthropic  # noqa: PLC0415  (optional dependency, imported on use)

    client = anthropic.Anthropic()
    response = client.messages.create(
        model=API_MODEL,
        max_tokens=16000,
        system=system,
        thinking={"type": "adaptive"},
        output_config={
            "effort": "medium",
            "format": {"type": "json_schema", "schema": schema},
        },
        messages=[{"role": "user", "content": prompt}],
    )
    for block in response.content:
        if block.type == "text":
            return block.text
    return ""


def parse(
    model: type[T],
    system: str,
    prompt: str,
    status: BackendStatus | None = None,
) -> T | None:
    """Ask the model for one object of type ``model``.

    Returns ``None`` when there is no backend, when the call fails, or when
    what came back does not validate. Every one of those is the same thing to
    the caller — no reasoned answer — and the caller already has to handle it,
    because the rules challenger runs either way.

    It deliberately does NOT retry a validation failure with a "try harder"
    prompt. That is how a model gets talked into inventing values to satisfy a
    schema, which is the exact failure the Abstention shape exists to avoid.
    """
    status = status or detect()
    if not status.available:
        return None

    schema = model.model_json_schema()
    try:
        if status.backend is Backend.LOCAL:
            raw = _call_local(system, prompt, schema)
        else:
            raw = _call_api(system, prompt, schema)
    except Exception as exc:  # noqa: BLE001 — any failure is "no answer"
        log.warning("model call failed (%s): %s", status.backend.value, exc)
        return None

    try:
        return model.model_validate_json(raw)
    except ValidationError as exc:
        log.warning("model returned something that does not validate: %s", exc)
        return None


def ask_text(
    system: str,
    prompt: str,
    status: BackendStatus | None = None,
) -> str | None:
    """Free-text answer, for the assistant. ``None`` when nothing is running."""
    status = status or detect()
    if not status.available:
        return None
    try:
        if status.backend is Backend.LOCAL:
            body = json.dumps({
                "model": LOCAL_MODEL,
                "stream": False,
                "options": {"temperature": 0.1},
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": prompt},
                ],
            }).encode()
            request = urllib.request.Request(
                f"{OLLAMA_HOST}/api/chat", data=body,
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(request, timeout=TIMEOUT_S) as response:
                return json.loads(response.read())["message"]["content"].strip()

        import anthropic  # noqa: PLC0415

        response = anthropic.Anthropic().messages.create(
            model=API_MODEL,
            max_tokens=16000,
            system=system,
            thinking={"type": "adaptive"},
            output_config={"effort": "low"},
            messages=[{"role": "user", "content": prompt}],
        )
        for block in response.content:
            if block.type == "text":
                return block.text.strip()
        return None
    except Exception as exc:  # noqa: BLE001
        log.warning("assistant call failed: %s", exc)
        return None


def report(status: BackendStatus | None = None) -> dict[str, Any]:
    """The socket line, for /api/inputs and the profile's Sources tab."""
    status = status or detect()
    return {
        "key": "reasoning_model",
        "label": "Reasoning model",
        "status": "connected" if status.available else "absent",
        "backend": status.backend.value,
        "model": status.model,
        "detail": status.detail,
        "unlocks_if_connected": status.unlocks_if_connected,
    }
