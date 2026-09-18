"""The unstructured path — news and social items (BRIEF §3.4).

SYNTHETIC DEMO CORPUS. Every item below is written by the build team to
exercise the pipeline. None is a real report. Each is labelled ``synthetic``,
each carries a verbatim quote so the provenance chain is real even when the
text is not, and the /inputs panel says plainly that the news socket is running
on a fixture.

Items are positioned RELATIVE TO THE AS-OF INSTANT rather than at fixed dates,
so a hindcast at a past as-of gets a corpus consistent with that date instead
of a pile of items from the future.

WHAT THE CORPUS IS FOR
----------------------
It is built to exercise the cases that matter, not to look impressive:

* items that gate onto real shipments, and items that gate onto NOTHING —
  the noise filter is the product, so it has to be visible working;
* a strike whose probability genuinely cannot be sourced, so the unsourced
  band gets used rather than being a theory;
* a tier-3 social signal that is later corroborated, so the promotion rule
  and the lead time it buys can be shown;
* a second-order item (Panama → Cape → Singapore) that no keyword router
  will resolve, which is where the reasoning layer earns its place.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from engine.clock import Clock
from engine.ingest.observations import FeedReport, FeedStatus


@dataclass(frozen=True)
class FeedItem:
    """One raw report, before resolution."""

    item_id: str
    headline: str
    body: str
    source: str
    source_tier: int
    published_offset_hours: float
    node_hint: list[str]
    lat: float | None
    lon: float | None
    starts_offset_hours: float
    ends_offset_hours: float | None
    probability: float | None
    probability_basis: str
    realized: bool
    synthetic: bool = True


# ---------------------------------------------------------------------
# The corpus
# ---------------------------------------------------------------------
CORPUS: tuple[FeedItem, ...] = (
    # --- gates onto the EU->US and EU->Asia lanes; P unsourceable ------
    FeedItem(
        item_id="SYN-NEWS-001",
        headline="Antwerp dockworkers vote to strike from Monday unless talks resume",
        body=(
            "Dockworkers at three Antwerp container terminals have voted in favour of "
            "industrial action beginning Monday unless talks with terminal operators "
            "resume before the weekend. Union representatives said the ballot returned "
            "a clear majority. Operators have not commented on contingency plans."
        ),
        source="synthetic_trade_press",
        source_tier=2,
        published_offset_hours=-14,
        node_hint=["BEANR"],
        lat=51.26,
        lon=4.40,
        starts_offset_hours=72,
        ends_offset_hours=None,  # duration genuinely unknown
        probability=None,  # "unless talks resume" IS the estimate; do not invent one
        probability_basis=(
            "conditional on talks resuming; no base rate available for this "
            "dispute — marked probability_unknown rather than guessed"
        ),
        realized=False,
    ),
    # --- tier 3, uncorroborated at first --------------------------------
    FeedItem(
        item_id="SYN-SOCIAL-002",
        headline="Reports of gate queues building at Rotterdam Maasvlakte",
        body=(
            "Several hauliers posting about long waiting time at the Maasvlakte gates "
            "this morning. Third day of congestion according to one driver. No official "
            "notice from the terminal yet."
        ),
        source="synthetic_social",
        source_tier=3,
        published_offset_hours=-6,
        node_hint=["NLRTM"],
        lat=51.95,
        lon=4.05,
        starts_offset_hours=-48,
        ends_offset_hours=96,
        probability=None,
        probability_basis="unverified social reports; below corroboration threshold",
        realized=True,
    ),
    # --- weather, P genuinely sourceable --------------------------------
    FeedItem(
        item_id="SYN-WX-003",
        headline="Gale warning issued for the southern North Sea",
        body=(
            "The national meteorological service has issued a gale warning for the "
            "southern North Sea and Dutch coastal waters from Thursday evening through "
            "Saturday, with storm force gusts expected. Container crane operations are "
            "likely to be suspended during the peak period."
        ),
        source="synthetic_met_service",
        source_tier=1,
        published_offset_hours=-3,
        node_hint=["NLRTM", "BEANR"],
        lat=52.2,
        lon=3.5,
        starts_offset_hours=54,
        ends_offset_hours=102,
        probability=0.72,
        probability_basis="forecast probability from the issuing met service",
        realized=False,
    ),
    # --- chokepoint; hits EU->Asia -------------------------------------
    FeedItem(
        item_id="SYN-NEWS-004",
        headline="Suez Canal Authority reduces daily transit slots on southbound convoy",
        body=(
            "The canal authority has confirmed a temporary reduction in daily transit "
            "slots for the southbound convoy over the next three weeks, citing "
            "maintenance on a section of the single-lane stretch. Vessels should expect "
            "extended waiting time at the northern anchorage."
        ),
        source="synthetic_authority",
        source_tier=1,
        published_offset_hours=-30,
        node_hint=["CHOKE_SUEZ"],
        lat=30.0,
        lon=32.55,
        starts_offset_hours=24,
        ends_offset_hours=504,
        probability=0.9,
        probability_basis="confirmed by the operating authority",
        realized=False,
    ),
    # --- second order: needs reasoning, defeats a keyword router --------
    FeedItem(
        item_id="SYN-NEWS-005",
        headline="Panama Canal cuts daily transits as reservoir levels fall",
        body=(
            "The Panama Canal Authority will reduce daily transits from next month as "
            "reservoir levels continue to fall. Analysts expect some Asia-US east coast "
            "volume to reroute via Suez or the Cape, with knock-on congestion at "
            "transshipment hubs including Singapore and Rotterdam in the weeks that "
            "follow."
        ),
        source="synthetic_trade_press",
        source_tier=2,
        published_offset_hours=-48,
        node_hint=["CHOKE_PANAMA"],
        lat=9.08,
        lon=-79.68,
        starts_offset_hours=240,
        ends_offset_hours=1440,
        probability=0.8,
        probability_basis="announced by the operating authority",
        realized=False,
    ),
    # --- rail, hits inland legs ------------------------------------------
    FeedItem(
        item_id="SYN-NEWS-006",
        headline="German rail union announces 48-hour freight strike",
        body=(
            "The rail union has announced a 48-hour strike affecting freight services "
            "across Germany from Wednesday morning. Freight paths are expected to be "
            "cancelled before passenger services and restored after them."
        ),
        source="synthetic_trade_press",
        source_tier=2,
        published_offset_hours=-20,
        node_hint=["DEHAM", "DEDUI", "SIKA_STU"],
        lat=51.0,
        lon=9.0,
        starts_offset_hours=96,
        ends_offset_hours=144,
        probability=0.85,
        probability_basis="strike formally announced with a stated duration",
        realized=False,
    ),
    # --- NOISE: real event, touches nothing of ours ----------------------
    FeedItem(
        item_id="SYN-NEWS-007",
        headline="Typhoon warning issued for the Philippine Sea",
        body=(
            "A typhoon is tracking north-west across the Philippine Sea with sustained "
            "winds expected to reach category three. Shipping in the area has been "
            "advised to seek shelter."
        ),
        source="synthetic_met_service",
        source_tier=1,
        published_offset_hours=-8,
        node_hint=[],
        lat=15.0,
        lon=130.0,
        starts_offset_hours=48,
        ends_offset_hours=144,
        probability=0.65,
        probability_basis="forecast probability from the issuing met service",
        realized=False,
    ),
    # --- NOISE: right place, wrong cargo ---------------------------------
    FeedItem(
        item_id="SYN-NEWS-008",
        headline="New EU import rules for food-grade packaging take effect next quarter",
        body=(
            "Importers of food-contact packaging materials face new documentation "
            "requirements from the start of next quarter. The rules do not apply to "
            "industrial chemical products."
        ),
        source="synthetic_regulatory",
        source_tier=1,
        published_offset_hours=-72,
        node_hint=["NLRTM"],
        lat=51.95,
        lon=4.14,
        starts_offset_hours=2160,
        ends_offset_hours=None,
        probability=1.0,
        probability_basis="published regulation with a stated commencement date",
        realized=False,
    ),
    # --- NOISE: right place, already over ---------------------------------
    FeedItem(
        item_id="SYN-NEWS-009",
        headline="Crane repairs completed at Genoa terminal",
        body=(
            "Repairs to the ship-to-shore crane out of service since last week have been "
            "completed and normal handling rates resumed yesterday."
        ),
        source="synthetic_trade_press",
        source_tier=2,
        published_offset_hours=-26,
        node_hint=["ITGOA"],
        lat=44.40,
        lon=8.92,
        starts_offset_hours=-336,
        ends_offset_hours=-24,  # ended before now: dies on the temporal gate
        probability=1.0,
        probability_basis="reported as complete",
        realized=True,
    ),
    # --- capacity -----------------------------------------------------------
    FeedItem(
        item_id="SYN-NEWS-010",
        headline="Carrier blanks two North Europe to Far East sailings",
        body=(
            "A carrier has confirmed it will blank two scheduled sailings on its North "
            "Europe to Far East service over the coming fortnight, citing soft demand. "
            "Cargo booked on the omitted sailings will roll to the following week."
        ),
        source="synthetic_carrier_advisory",
        source_tier=2,
        published_offset_hours=-40,
        node_hint=["NLRTM", "BEANR", "DEHAM"],
        lat=51.95,
        lon=4.14,
        starts_offset_hours=120,
        ends_offset_hours=456,
        probability=0.95,
        probability_basis="confirmed in a carrier advisory",
        realized=False,
    ),
)


def load_feed_items(clock: Clock) -> tuple[list[dict], FeedReport]:
    """Materialise the corpus against the as-of instant."""
    items: list[dict] = []
    for item in CORPUS:
        published = clock.as_of + timedelta(hours=item.published_offset_hours)
        if published > clock.as_of:
            continue  # not yet published at this as-of

        starts = clock.as_of + timedelta(hours=item.starts_offset_hours)
        ends = (
            clock.as_of + timedelta(hours=item.ends_offset_hours)
            if item.ends_offset_hours is not None
            else None
        )
        items.append(
            {
                "item_id": item.item_id,
                "headline": item.headline,
                "body": item.body,
                "text": f"{item.headline}. {item.body}",
                "source": item.source,
                "source_tier": item.source_tier,
                "published_at": published,
                "node_hint": list(item.node_hint),
                "lat": item.lat,
                "lon": item.lon,
                "starts_at": starts,
                "ends_at": ends,
                "probability": item.probability,
                "probability_basis": item.probability_basis,
                "realized": item.realized,
                "synthetic": item.synthetic,
            }
        )

    report = FeedReport(
        key="news_feed",
        label="News / trade press (RSS)",
        status=FeedStatus.FIXTURE,
        detail=(
            f"{len(items)} synthetic items, written to exercise the gate "
            "(including items that correctly match nothing)"
        ),
        unlocks_if_connected=(
            "Real RSS over trade press and authority notices. The gate, the "
            "router and the scoring are unchanged — only the corpus differs."
        ),
        records=len(items),
        retrieved_at=clock.as_of,
        source_tier=2,
    )
    return items, report


def social_promotion_status(
    item: dict,
    all_items: list[dict],
    thresholds: dict,
) -> tuple[bool, str]:
    """Has a tier-3 signal earned promotion? (answers Sika Q5)

    Sika: *"let's factor in social media, in line with your suggested
    approach."* The proposal was that official sources drive the advisory logic
    while social is carried alongside as supporting context, entering only on
    corroboration.

    The point worth making out loud: social media is not a *better* signal, it
    is an *earlier* one. Its entire value is that it arrives while options are
    still cheap — which is a lead-time argument, and therefore lives on the
    option-decay curve rather than in the accuracy story.
    """
    tiers = thresholds.get("source_tiers", {})
    tier = item.get("source_tier", 3)
    spec = tiers.get(tier) or tiers.get(str(tier))
    if not spec or spec.get("can_trip_convene", True):
        return True, "source tier drives advisory logic directly"

    rule = spec.get("corroboration", {})
    needed = rule.get("independent_accounts", 3)
    confirm_tier = rule.get("or_confirmed_by_tier", 2)

    # Corroboration requires the same NODE *and* the same kind of claim. A gale
    # warning at Rotterdam does not corroborate a report of gate queues at
    # Rotterdam; they are two different assertions that happen to share a
    # coordinate. Matching on location alone promotes almost anything at a busy
    # node, which would hand a tier-3 rumour the authority of a met office.
    def _same_claim(other: dict) -> bool:
        if not (set(other["node_hint"]) & set(item["node_hint"])):
            return False
        mine = set(item.get("active_variables") or [])
        theirs = set(other.get("active_variables") or [])
        if mine and theirs:
            return bool(mine & theirs)
        # No routed variables yet: fall back to headline overlap on content
        # words, which is crude but at least topical.
        stop = {"the", "at", "in", "of", "to", "and", "for", "on", "as", "from", "a"}
        mine_w = {w for w in item["headline"].lower().split() if w not in stop}
        theirs_w = {w for w in other["headline"].lower().split() if w not in stop}
        return len(mine_w & theirs_w) >= 2

    nearby = [
        other for other in all_items
        if other["item_id"] != item["item_id"] and _same_claim(other)
    ]
    official = [o for o in nearby if o.get("source_tier", 3) <= confirm_tier]
    if official:
        return True, (
            f"corroborated by a tier-{official[0]['source_tier']} source "
            f"({official[0]['source']})"
        )

    same_tier = [o for o in nearby if o.get("source_tier", 3) == tier]
    if len(same_tier) + 1 >= needed:
        return True, f"{len(same_tier) + 1} independent accounts within the window"

    return False, (
        f"unconfirmed — {len(same_tier) + 1} of {needed} independent accounts, "
        "no official source yet. Visible, but cannot drive a decision."
    )
