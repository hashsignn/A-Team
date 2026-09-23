"""What a source IS, before any of them exist.

THE POINT OF THIS FILE
======================
Adding a feed should be a block of YAML, not a Python file. Ten hand-written
HTTP clients is ten places to fix a timeout bug and ten things Sika would have
to read before adding an eleventh. So every source — ours and theirs — is a
``SourceSpec``: where to fetch, how to find the items inside the response, and
which field of each item means what.

The engine never imports a source. It imports this.

NATURE: THE DISTINCTION THAT DECIDES WHO PAYS FOR A MODEL CALL
--------------------------------------------------------------
Sources split in two, and the split is not cosmetic:

``INSTRUMENT``
    Something measured a number. A gauge at Kaub reads 78 cm. A seismometer
    reads M6.1. There is nothing to interpret — the reading maps to a variable
    by a threshold table, deterministically, for free. **These never reach a
    model**, and running one on them would be pure cost with a chance of being
    wrong about arithmetic.

``REPORT``
    Somebody *said* a thing. A wire story, a notice, a tweet. The claim needs
    reading: is it about freight, where, when, how long, and is the stated
    probability sourceable or invented. **This is what the funnel is for.**

That distinction is the whole reason the model bill is small. A weather API
that predicts wind speed is an instrument and costs nothing to consume; a wire
report that a strait has been closed is the thing no API predicts, and it is
worth a model call precisely because no threshold table can read it.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from enum import Enum

# English month names, spelled out rather than taken from strftime("%B"),
# which follows the machine's locale: a page asked for as "2026 septembre 21"
# does not exist.
MONTH_NAMES = (
    "January", "February", "March", "April", "May", "June", "July",
    "August", "September", "October", "November", "December",
)


class Nature(str, Enum):
    """See the module docstring. This field decides the cost of a source."""

    INSTRUMENT = "instrument"
    REPORT = "report"


class Cost(str, Enum):
    """What this source costs to consume, stated so nobody finds out later."""

    FREE = "free"                  # no key, no registration, no bill
    FREE_WITH_KEY = "free_with_key"  # registration required, still no bill
    PAID = "paid"                  # never enabled by default


@dataclass(frozen=True)
class Auth:
    """How to authenticate, WITHOUT the credential.

    The credential lives in an environment variable and is read at fetch time.
    A spec carrying a token would put it in the repo the moment somebody
    committed their config, which is the failure ``.gitignore`` alone does not
    prevent — a value that is never in the file cannot leak from the file.
    """

    kind: str = "none"        # none | bearer | header | query
    env: str = ""             # environment variable holding the secret
    name: str = ""            # header or query-parameter name

    def resolve(self) -> str | None:
        """The secret, or None. Never logged, never returned in a report."""
        return os.environ.get(self.env) if self.env else None

    @property
    def configured(self) -> bool:
        return self.kind == "none" or bool(self.resolve())


@dataclass(frozen=True)
class FieldMap:
    """Where each part of a FeedItem lives inside one raw item.

    Values are dotted paths (``properties.title``, ``geometry.coordinates[1]``)
    resolved by ``mapping.py``. A path that does not resolve yields None rather
    than raising: a source that adds a field must not break one that does not
    have it.

    ``const:`` prefix supplies a literal instead of a path, which is how a
    source states its own tier or a fixed node hint.
    """

    headline: str
    body: str = ""
    published: str = ""
    lat: str = ""
    lon: str = ""
    url: str = ""
    source_name: str = ""
    starts: str = ""
    ends: str = ""
    node_hint: str = ""
    severity_hint: str = ""
    identifier: str = ""


@dataclass(frozen=True)
class Window:
    """How to ask a source for a PAST window rather than "the last few hours".

    Without this the fetcher can only ever ask for now. That is fine for a
    live board and wrong for two things the product needs: replaying a
    specific day for a demo, and recording the reasoning layer over a period
    that already happened. Both were impossible while every request meant
    "the last 3 days from whenever you happen to be running this".

    ``days`` is how far back from the as-of to ask. It is per source because
    the right window differs: a news index over two months is a corpus, the
    same span of motorway closures is mostly noise about roadworks that
    reopened.
    """

    start_param: str
    end_param: str = ""
    # "gdelt" -> YYYYMMDDHHMMSS, "iso" -> 2026-09-16T00:00:00Z,
    # "date" -> 2026-09-16, "epoch" -> seconds, or "template:..." for a
    # source asked by NAME rather than by time: Wikipedia's
    # "Portal:Current events/{year} {month_name} {day}" is one page per day.
    format: str = "iso"
    days: float = 3.0
    # Params to DROP when a window is asked for. GDELT rejects `timespan`
    # alongside `startdatetime`, and a request carrying both silently returns
    # the relative window — which looks like the historical query working.
    drops: tuple[str, ...] = ()

    def stamp(self, moment) -> str:
        if self.format.startswith("template:"):
            return self.format[len("template:"):].format(
                year=moment.year, month=moment.month,
                month_name=MONTH_NAMES[moment.month - 1], day=moment.day,
            )
        if self.format == "gdelt":
            return moment.strftime("%Y%m%d%H%M%S")
        if self.format == "date":
            return moment.strftime("%Y-%m-%d")
        if self.format == "epoch":
            return str(int(moment.timestamp()))
        return moment.strftime("%Y-%m-%dT%H:%M:%SZ")

    def params_for(self, as_of, days: float | None = None) -> tuple[dict, tuple]:
        """(params to add, params to drop) for a window ending at ``as_of``."""
        from datetime import timedelta

        span = self.days if days is None else days
        start = as_of - timedelta(days=span)
        out = {self.start_param: self.stamp(start)}
        if self.end_param:
            out[self.end_param] = self.stamp(as_of)
        return out, self.drops


@dataclass(frozen=True)
class SourceSpec:
    """One feed, declaratively.

    Everything the fetcher, the mapper and the /inputs panel need, and nothing
    about how the engine will use it.
    """

    key: str
    label: str
    nature: Nature
    source_tier: int
    url: str
    items_path: str
    fields: FieldMap
    cost: Cost = Cost.FREE
    unlocks_if_connected: str = ""
    fixture: str = ""
    auth: Auth = field(default_factory=Auth)
    params: dict[str, str] = field(default_factory=dict)
    headers: dict[str, str] = field(default_factory=dict)
    date_format: str = "iso"
    # For a source whose answer is not yet a list of items — Wikipedia answers
    # with a page of wikitext. Names a decoder in sources/decoders.py that
    # turns the answer into the list ``items_path`` then finds. Empty for
    # every other source.
    decode: str = ""
    # Set when the source can be asked for a past window. None means it only
    # ever answers about now, which the /inputs panel says rather than hiding.
    window: Window | None = None
    families: tuple[str, ...] = ()

    # Modes this source can POSSIBLY be reporting on. A motorway closure feed
    # cannot tell you anything about a barge, so an item from it must never
    # produce a barge event — however generic the variable it matched.
    #
    # This is a real failure, not a hypothetical: "A5 | Karlsruhe -> Basel
    # closed after HGV fire" matches FOR_FIRE, which is declared for every
    # mode, and the fire then appeared on a Rhine barge leg. The variable is
    # right to be generic; the SOURCE is what knows better.
    #
    # Empty means "no restriction", which is correct for a news index.
    modes: tuple[str, ...] = ()

    enabled: bool = True
    builtin: bool = False
    notes: str = ""

    def __post_init__(self) -> None:
        if not _KEY.match(self.key):
            raise ValueError(
                f"source key {self.key!r} must be lowercase letters, digits and "
                "underscores — it becomes a FeedReport key and an item id prefix"
            )
        if self.source_tier not in (1, 2, 3):
            raise ValueError(
                f"{self.key}: source_tier must be 1, 2 or 3 — the corroboration "
                f"cap reads it, got {self.source_tier!r}"
            )

    @property
    def resolved_url(self) -> str:
        """The URL with ``${VAR}`` expanded from the environment.

        Lets a custom source point at an internal host without that hostname
        being committed — Sika's TMS endpoint is not our business to store.
        """
        return _expand(self.url)

    @property
    def runnable(self) -> bool:
        """Enabled, paid-for if it must be, and holding any key it needs."""
        return self.enabled and self.cost is not Cost.PAID and self.auth.configured

    def why_not_runnable(self) -> str:
        if not self.enabled:
            return "disabled in sources.yaml"
        if self.cost is Cost.PAID:
            return "paid source — never enabled by default"
        if not self.auth.configured:
            return f"needs {self.auth.env} in the environment"
        return ""


_KEY = re.compile(r"^[a-z][a-z0-9_]*$")
_VAR = re.compile(r"\$\{([A-Z_][A-Z0-9_]*)\}")


def _expand(text: str) -> str:
    return _VAR.sub(lambda m: os.environ.get(m.group(1), ""), text)
