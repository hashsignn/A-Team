"""Nothing in this build calls anything that bills.

This is a finished prototype. Running it — on a laptop, in a Codespace, on a
stage — must cost nothing, and "must" is meant literally: not "costs nothing
unless somebody's environment happens to hold a key".

Every paid service it could use is still DECLARED. The reasoning model's
paid API, a paid data source, an outbound webhook to a carrier: each appears
in the source panel or the model status as something that CAN be attached,
with what attaching it would add, because showing that the socket exists is
part of the design. None of them is connected.

WHY A CONSTANT AND NOT AN ENVIRONMENT VARIABLE
==============================================
An environment variable gets set for other reasons. Somebody who uses the
Anthropic API elsewhere has ANTHROPIC_API_KEY in their shell; a Codespaces
secret is shared across every repository on an account. Before this module
existed either was enough: with no local model running, the board switched
itself onto the paid API, and the first anyone would have heard of it was the
invoice. Connecting a paid service here now means changing the line below,
which goes through review — and a test fails, saying why, the moment it does.

WHAT IT COVERS
==============
    reasoning model   the Anthropic API — engine/reason/llm.py
    data sources      a source declared `cost: paid` — engine/ingest/sources
                      (never runnable; see SourceSpec.runnable)
    dispatch          webhook and API channels ship disabled, and a disabled
                      channel records instead of sending — engine/fast/dispatch

Free things are untouched: a local model through Ollama, the keyless public
feeds, and the in-process socket that talks to the browser.
"""

from __future__ import annotations

PAID_SERVICES_CONNECTED = False


class PaidServiceNotConnected(RuntimeError):
    """Raised by any code path that would have billed, before it could."""


def not_connected(what: str) -> str:
    """The sentence every paid socket uses to say why it is idle."""
    return (
        f"{what} bills per use, so it is not connected in this prototype. It "
        "is shown so it can be attached; attaching it is a reviewed change to "
        "engine/costs.py, not a setting."
    )


def refuse(what: str) -> None:
    """Stop a paid call before it is made. Does nothing once connected."""
    if not PAID_SERVICES_CONNECTED:
        raise PaidServiceNotConnected(not_connected(what))
