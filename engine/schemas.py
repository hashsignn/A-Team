"""Every Pydantic model, one file (BRIEF §9.2).

Two disciplines from BRIEF §8.1 are enforced here rather than by convention:

**An absence never becomes a value.** Where a quantity may genuinely be
unknown, the model carries an explicit ``None`` plus a reason, and the
renderer is obliged to say "unassessed" rather than draw a 0.5. The sibling
project shipped a confident 0.50 across six dimensions because a field was
optional-with-default; there are no optional-with-default fields on anything a
model fills.

**Abstention is a valid answer, structurally.** ``Extraction`` requires every
field. Requiring every field while also requiring an answer is a hallucination
generator: the model will invent values to satisfy the schema. So the reasoning
layer returns ``Extraction | Abstention`` as a discriminated union, and
"I could not tell" is a shape the parser accepts rather than a failure it
retries into a guess.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from engine.clock import ensure_utc

# =====================================================================
# Enumerations
# =====================================================================


class Mode(str, Enum):
    ROAD = "road"
    RAIL = "rail"
    SEA = "sea"
    BARGE = "barge"


class NodeKind(str, Enum):
    PLANT = "plant"
    DISTRIBUTION = "distribution"
    SEAPORT = "seaport"
    INLAND_PORT = "inland_port"
    CHOKEPOINT = "chokepoint"
    GAUGE = "gauge"
    CUSTOMER = "customer"


class Severity(str, Enum):
    MINOR = "minor"
    MODERATE = "moderate"
    SEVERE = "severe"


class ContractType(str, Enum):
    AGREEMENT = "agreement"
    SPOT = "spot"


class CustomerImpactTier(str, Enum):
    LINE_DOWN = "line_down"
    STOCK_OUT = "stock_out"
    INCONVENIENCE = "inconvenience"


class Posture(str, Enum):
    NORMAL = "normal"
    WATCH = "watch"
    CONVENE = "convene"


# =====================================================================
# Network
# =====================================================================


class Node(BaseModel):
    """A place freight passes through.

    BRIEF §4 is emphatic that nodes must NOT carry a 100-dimensional
    vulnerability vector. Exposure is derived from these few readable
    attributes instead — see engine/variables/mask.py.
    """

    model_config = ConfigDict(frozen=True)

    id: str
    name: str
    kind: NodeKind
    country: str
    lat: float
    lon: float
    modes: list[Mode]
    on_river: bool = False
    river: str | None = None
    inland: bool = False
    chokepoint: bool = False
    throughput_share: float = 0.0
    alternatives: list[str] = Field(default_factory=list)

    @property
    def is_port(self) -> bool:
        return self.kind in (NodeKind.SEAPORT, NodeKind.INLAND_PORT)


class Leg(BaseModel):
    """One ordered hop of a shipment.

    DEVIATION from BRIEF §3.3, and a necessary one. The brief's schema carries
    only shipment-level ``etd``/``eta``. The gate's temporal condition asks
    whether an event window overlaps the shipment's transit *through that
    node*, which is unanswerable without per-leg windows. Every leg therefore
    carries its own planned departure, planned arrival and buffer.
    """

    model_config = ConfigDict(frozen=True)

    from_node: str
    to_node: str
    mode: Mode
    planned_depart: datetime
    planned_arrive: datetime
    buffer_hours: float
    carrier: str

    @field_validator("planned_depart", "planned_arrive")
    @classmethod
    def _utc(cls, v: datetime) -> datetime:
        return ensure_utc(v)

    @property
    def transit_hours(self) -> float:
        return (self.planned_arrive - self.planned_depart).total_seconds() / 3600.0


class Shipment(BaseModel):
    model_config = ConfigDict(frozen=True)

    shipment_id: str
    lane_id: str
    origin_node: str
    destination_node: str
    mode: Literal["road", "rail", "sea", "barge", "multimodal"]
    legs: list[Leg]
    carrier: str
    contract_type: ContractType
    etd: datetime
    eta: datetime
    otif_committed_date: datetime
    value_chf: float
    product_family: str
    customer: str
    customer_impact_tier: CustomerImpactTier
    sla_penalty_per_day: float
    dangerous_goods: bool
    temperature_controlled: bool
    synthetic: bool = True

    @field_validator("etd", "eta", "otif_committed_date")
    @classmethod
    def _utc(cls, v: datetime) -> datetime:
        return ensure_utc(v)

    @property
    def node_ids(self) -> list[str]:
        seen: list[str] = []
        for leg in self.legs:
            for nid in (leg.from_node, leg.to_node):
                if nid not in seen:
                    seen.append(nid)
        return seen

    @property
    def commitment_slack_hours(self) -> float:
        """Hours between the planned arrival and the date promised to the
        customer. Delay inside this window costs nothing contractually — which
        is exactly the distinction BRIEF §5.4's loss formula loses."""
        return (self.otif_committed_date - self.eta).total_seconds() / 3600.0


# =====================================================================
# Risk variables
# =====================================================================


class RiskVariable(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    family: str
    name: str
    description: str
    modes_affected: list[Mode]
    typical_lead_time_hours: float
    typical_duration_days: float
    exposure: dict
    probability_sourceable: bool


class DelayTriple(BaseModel):
    """Three-point estimate in days (BRIEF §5.2)."""

    model_config = ConfigDict(frozen=True)

    optimistic: float
    likely: float
    pessimistic: float

    @field_validator("pessimistic")
    @classmethod
    def _ordered(cls, v: float, info) -> float:
        opt = info.data.get("optimistic", 0.0)
        lik = info.data.get("likely", 0.0)
        if not (opt <= lik <= v):
            raise ValueError(
                f"triangular estimate must be ordered: got "
                f"optimistic={opt}, likely={lik}, pessimistic={v}"
            )
        return v


# =====================================================================
# Events
# =====================================================================


class Provenance(BaseModel):
    """Where a value came from (BRIEF §8.4).

    A value with neither a quote nor an explicit ``inferred`` marker is a bug,
    and is rendered as one.
    """

    model_config = ConfigDict(frozen=True)

    source: str
    source_tier: int
    verbatim_quote: str | None
    inferred: bool
    retrieved_at: datetime
    url: str | None = None

    @field_validator("retrieved_at")
    @classmethod
    def _utc(cls, v: datetime) -> datetime:
        return ensure_utc(v)

    @property
    def is_grounded(self) -> bool:
        return bool(self.verbatim_quote) or self.inferred


class Event(BaseModel):
    """A resolved disruption: many reports, one event (BRIEF §3.5)."""

    event_id: str
    title: str
    node_ids: list[str]
    lat: float | None
    lon: float | None
    starts_at: datetime
    ends_at: datetime | None
    duration_confidence: Literal["stated", "estimated", "unknown"]
    event_class: str
    active_variables: list[str]
    severity: Severity
    modes_affected: list[Mode]
    realized: bool

    # P is None when it genuinely cannot be sourced. BRIEF §5.3: do not invent
    # the last one — a made-up 0.6 looks like evidence.
    probability: float | None
    probability_basis: str

    provenance: Provenance
    second_order_nodes: list[str] = Field(default_factory=list)
    payload_fraction: float | None = None  # capacity derate, e.g. Rhine low water
    cost_multiplier: float = 1.0  # e.g. low-water surcharge
    confidence: float = 1.0

    @field_validator("starts_at")
    @classmethod
    def _utc_start(cls, v: datetime) -> datetime:
        return ensure_utc(v)

    @field_validator("ends_at")
    @classmethod
    def _utc_end(cls, v: datetime | None) -> datetime | None:
        return ensure_utc(v) if v is not None else None

    @property
    def probability_known(self) -> bool:
        return self.probability is not None

    def window(self, fallback_days: float) -> tuple[datetime, datetime]:
        """Event window, using a stated end where there is one.

        An unknown end is widened by *fallback_days* rather than treated as
        instantaneous. Under-merging costs a duplicate row a planner dismisses;
        missing a live disruption costs the thing the tool exists to prevent.
        """
        from datetime import timedelta

        if self.ends_at is not None:
            return self.starts_at, self.ends_at
        return self.starts_at, self.starts_at + timedelta(days=fallback_days)


# =====================================================================
# Gate
# =====================================================================


class GateHit(BaseModel):
    """One (event, shipment, leg) intersection that survived the gate.

    Carries the reason each of the three conditions passed, so the planner can
    be shown why this shipment is on the list rather than asked to trust it.
    """

    event_id: str
    shipment_id: str
    leg_index: int
    node_id: str
    mode: Mode
    spatial_reason: str
    temporal_reason: str
    modal_reason: str
    leg_enters_at: datetime
    leg_leaves_at: datetime


# =====================================================================
# Simulation & scoring
# =====================================================================


class ShipmentOutcome(BaseModel):
    """Monte Carlo result for one shipment under one scenario."""

    shipment_id: str
    scenario: Literal["do_nothing", "act"]
    p_late: float
    expected_delay_days: float
    p90_delay_days: float
    expected_lateness_days: float
    expected_loss_chf: float
    p90_loss_chf: float
    driving_event_ids: list[str]


class ActionOption(BaseModel):
    """One thing the planner could actually do."""

    action_id: str
    label: str
    action_type: str
    owner: Literal["us", "carrier", "customer"]
    min_hours: float
    cost_chf: float
    residual_delay_days: float
    feasible: bool
    infeasible_reason: str | None
    contacts: list[str]
    description: str


class ShipmentRisk(BaseModel):
    """Everything computed about one shipment under one event."""

    shipment_id: str
    event_id: str
    do_nothing: ShipmentOutcome
    best_action: ActionOption | None
    act_outcome: ShipmentOutcome | None
    value_of_acting_chf: float
    decision_deadline: datetime | None
    lead_time_hours: float | None
    actionability: Literal["comfortable", "tightening", "too_late", "no_action"]
    impact_band: str
    probability_band: str
    value_chf: float
    customer: str
    contract_type: ContractType

    @field_validator("decision_deadline")
    @classmethod
    def _utc(cls, v: datetime | None) -> datetime | None:
        return ensure_utc(v) if v is not None else None


class EventAssessment(BaseModel):
    """An event plus every shipment it threatens — the per-event matrix."""

    event: Event
    shipment_risks: list[ShipmentRisk]
    total_value_at_risk_chf: float
    total_value_of_acting_chf: float
    shipments_affected: int
    contracts_affected: list[str]
    max_priority_chf: float
    variable_contributions: dict[str, float]  # variable id -> days of delay


# =====================================================================
# Portfolio — the convene decision
# =====================================================================


class DecayPoint(BaseModel):
    """One sample of the option-decay curve."""

    at: datetime
    hours_from_now: float
    recoverable_chf: float
    recoverable_p10_chf: float
    recoverable_p90_chf: float
    actions_still_open: int
    shipments_still_actionable: int
    expiring_next: list[str]

    @field_validator("at")
    @classmethod
    def _utc(cls, v: datetime) -> datetime:
        return ensure_utc(v)


class ConveneVerdict(BaseModel):
    """Whether the pre-agreed rule has tripped.

    The tool does not argue for a crisis. It reports that a threshold the team
    agreed in calm conditions has been crossed — which is what removes the
    political cost from the person who would otherwise have to stick their neck
    out. That, per Sika's own answer to Q6, is the actual failure mode.
    """

    posture: Posture
    rule_agreed: bool
    triggers_fired: list[str]
    # Every trigger is a quantity a planner can see and check. None depends on
    # the summed value of acting, which rests on our invented action costs.
    exposure_chf: float
    contracts_exposed: int
    options_expiring: int
    next_meeting_at: datetime | None
    headline: str

    @field_validator("next_meeting_at")
    @classmethod
    def _utc(cls, v: datetime | None) -> datetime | None:
        return ensure_utc(v) if v is not None else None


class FunnelCounts(BaseModel):
    """Measured ingestion funnel (BRIEF §3.2).

    The brief's 10,000 -> 500 -> 100 -> 60 -> 20 is a design illustration. These
    are the real counts for the run just executed, so the /inputs panel shows a
    measurement instead of a claim.
    """

    raw_observations: int
    after_geographic: int
    after_type: int
    after_temporal: int
    after_resolution: int
    reasoned: int
    gated_hits: int
    shipments_touched: int


class PipelineResult(BaseModel):
    """What one run of the pipeline produces."""

    as_of: datetime
    shipments_total: int
    events_total: int
    assessments: list[EventAssessment]
    decay_curve: list[DecayPoint]
    convene: ConveneVerdict
    funnel: FunnelCounts
    config_version: str

    @field_validator("as_of")
    @classmethod
    def _utc(cls, v: datetime) -> datetime:
        return ensure_utc(v)


# =====================================================================
# Reasoning layer — the discriminated union
# =====================================================================


class Extraction(BaseModel):
    """A reasoned reading of one unstructured report.

    Every field required, every field carrying its justification (BRIEF §6.2).
    """

    model_config = ConfigDict(extra="forbid")

    kind: Literal["extraction"] = "extraction"
    what_happened: str
    event_class: str
    location_text: str
    resolved_node_ids: list[str]
    starts_at: datetime
    ends_at: datetime | None
    duration_confidence: Literal["stated", "estimated", "unknown"]
    realized: bool
    probability: float | None
    probability_basis: str
    delay_days: DelayTriple
    delay_reasoning: str
    active_variables: list[str]
    why_active: dict[str, str]
    second_order_nodes: list[str]
    verbatim_quote: str
    confidence: float


class Abstention(BaseModel):
    """The model declining to answer.

    This exists so that "I cannot tell" has a shape the parser accepts. Without
    it, "every field is required" plus "you must produce output" means the model
    invents values to satisfy the schema — which is the failure this whole
    discipline is trying to avoid.
    """

    model_config = ConfigDict(extra="forbid")

    kind: Literal["abstention"] = "abstention"
    abstain_reason: str
    partial_note: str | None


ReasonedOutput = Annotated[
    Extraction | Abstention,
    Field(discriminator="kind"),
]
