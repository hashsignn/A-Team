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

from engine import costs
from engine.reason import cache

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

# The triage model. Deliberately small: stage 1 answers one yes/no question
# over hundreds of headlines, and a 7B reading each of them is most of the
# cost of the funnel for none of the judgement. Falls back to LOCAL_MODEL
# when unset, so a single-model install still works.
TRIAGE_MODEL = os.environ.get("RADAR_TRIAGE_MODEL", "") or LOCAL_MODEL

# The extraction model. Where the hard reading happens: a conditional
# ("unless talks resume"), a second-order effect, a stated duration that
# contradicts the headline.
EXTRACT_MODEL = os.environ.get("RADAR_EXTRACT_MODEL", "") or LOCAL_MODEL

# The API path. DECLARED, not connected: it bills per call, and this build
# never calls anything that bills — see engine/costs.py. Kept so the socket is
# visible and so attaching it is one reviewed line rather than a rewrite.
# Opus 5 because it would serve the judgement-heavy end, not the bulk end.
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

    The paid API is never chosen while ``costs.PAID_SERVICES_CONNECTED`` is
    False — not by default, not by ``RADAR_LLM_BACKEND=api``, and not because
    ``ANTHROPIC_API_KEY`` happens to be in the environment. It used to be
    chosen by that last one alone whenever no local model was running, which
    meant a Codespace with a key in its secrets billed for every board.
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

    if (
        choice in ("", Backend.API.value)
        and _has_api_key()
        and costs.PAID_SERVICES_CONNECTED
    ):
        return BackendStatus(
            Backend.API, API_MODEL,
            f"Anthropic API, model {API_MODEL}",
            "",
        )

    paid = costs.not_connected("The Anthropic API")
    if choice == Backend.LOCAL.value:
        detail = f"Ollama not reachable at {OLLAMA_HOST}"
    elif choice == Backend.API.value:
        detail = paid
    elif _has_api_key():
        detail = (
            f"no Ollama at {OLLAMA_HOST}. ANTHROPIC_API_KEY is set, and is "
            f"ignored: {paid}"
        )
    else:
        detail = f"no Ollama at {OLLAMA_HOST}"

    return BackendStatus(
        Backend.NONE, None, detail,
        "Reading the conditional in “unless talks resume”, the duration in "
        "“at least ten days”, and second-order effects the keyword router "
        "cannot see. A local model through Ollama adds this for free; the "
        "Anthropic API could be attached but bills per call, so it is not "
        "connected in this prototype. The router still produces a complete "
        "board without either.",
    )


# ---------------------------------------------------------------------
# Calling
# ---------------------------------------------------------------------
def _call_local(system: str, prompt: str, schema: dict, model: str = "") -> str:
    """Ollama's /api/chat with structured output.

    Plain urllib: this is one POST to localhost, and a dependency for that is
    a dependency the planner has to install to run the demo.
    """
    body = json.dumps({
        "model": model or LOCAL_MODEL,
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


def _call_api(system: str, prompt: str, schema: dict, model: str = "") -> str:
    """The Anthropic SDK, imported lazily — and refused before the import.

    Declared, not connected: this bills per call, and nothing in this build
    calls anything that bills (engine/costs.py). The refusal is here as well
    as in ``detect`` because ``parse`` takes a ``status`` from its caller, so
    a caller that builds its own could otherwise reach this line directly.

    The SDK is in no requirements file; it would be installed only by
    whoever attaches this path.
    """
    costs.refuse("The Anthropic API")

    import anthropic  # noqa: PLC0415  (optional dependency, imported on use)

    client = anthropic.Anthropic()
    response = client.messages.create(
        model=model or API_MODEL,
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
    model_name: str = "",
) -> T | None:
    """Ask a model, now. One object of type ``model``, or None.

    Returns ``None`` when there is no backend, when the call fails, or when
    what came back does not validate. Every one of those is the same thing to
    the caller — no reasoned answer — and the caller already has to handle it,
    because the rules challenger runs either way.

    It deliberately does NOT retry a validation failure with a "try harder"
    prompt. That is how a model gets talked into inventing values to satisfy a
    schema, which is the exact failure the Abstention shape exists to avoid.

    This is the LIVE call and nothing else. Recording and replay sit above it
    in ``parse_with_provenance``, so that "ask the model" stays one small
    function with one job — which is also what lets a test stub the model by
    replacing this and nothing else.
    """
    status = status or detect()
    if not status.available:
        return None

    schema = model.model_json_schema()
    try:
        if status.backend is Backend.LOCAL:
            raw = _call_local(system, prompt, schema, model_name)
        else:
            raw = _call_api(system, prompt, schema, model_name)
    except Exception as exc:  # noqa: BLE001 — any failure is "no answer"
        log.warning("model call failed (%s): %s", status.backend.value, exc)
        return None

    try:
        return model.model_validate_json(raw)
    except ValidationError as exc:
        log.warning("model returned something that does not validate: %s", exc)
        return None


def parse_with_provenance(
    model: type[T],
    system: str,
    prompt: str,
    status: BackendStatus | None = None,
    model_name: str = "",
    stage: str = "",
) -> tuple[T | None, str]:
    """An answer, and where it came from.

    Three origins, and the caller needs to tell them apart: ``live`` ran a
    model now, ``replayed`` read an answer recorded earlier on a machine that
    had one, and ``""`` means there was no answer at all. A replayed answer
    presented as a live one is an answer whose age is invisible, which is the
    one thing the provenance discipline here exists to prevent.
    """
    # The recording is checked BEFORE the backend, not as a fallback after it
    # fails. A recorded answer to this exact question is the answer a live call
    # would give, and paying for it twice is the point of having recorded it.
    if stage:
        hit = cache.lookup(stage, system, prompt)
        if hit is not None:
            try:
                return model.model_validate(hit.payload), f"replayed:{hit.provenance}"
            except ValidationError as exc:
                # A recording that no longer fits the schema is stale, not
                # authoritative. Fall through to a live call if one is possible.
                log.warning("recorded answer no longer validates: %s", exc)

    status = status or detect()
    answer = parse(model, system, prompt, status=status, model_name=model_name)
    if answer is None:
        return None, ""

    if stage and cache.recording():
        cache.record(
            stage, system, prompt, answer.model_dump(mode="json"),
            model=model_name or status.model or "unnamed",
            at=_stamp(),
        )
    return answer, "live"


def _stamp() -> str:
    """When a recording was made.

    Through Clock.wall() rather than datetime.now, even though this stamps an
    artefact on disk rather than a number on the board. The rule that the
    engine reads the wall clock in exactly one place is worth more than the
    one import it would save here, and a test enforces it — which is how this
    line got written correctly on the second attempt rather than the fifth.
    """
    from engine.clock import Clock

    return Clock.wall().as_of.isoformat()


def ask_text(
    system: str,
    prompt: str,
    status: BackendStatus | None = None,
    model: str = "",
) -> str | None:
    """Free-text answer, for the assistant. ``None`` when nothing is running."""
    status = status or detect()
    if not status.available:
        return None
    try:
        if status.backend is Backend.LOCAL:
            body = json.dumps({
                "model": model or LOCAL_MODEL,
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

        # The assistant's own paid path — refused for the same reason and in
        # the same place as _call_api: the status here is the caller's, and
        # a question typed into the Ask box must not be able to bill.
        costs.refuse("The Anthropic API")

        import anthropic  # noqa: PLC0415

        response = anthropic.Anthropic().messages.create(
            model=model or API_MODEL,
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
