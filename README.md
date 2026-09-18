# Supply Chain Risk Radar

> A supply chain planner at Sika is tracking 50–200 active shipments across
> road, rail and sea. The world produces thousands of external events a day.
> Almost none of them matter. **Tell the planner which ones do, early enough to
> act, and what to do about it.**

Built for the Sika Innovathon 2026 — *"How can supply chain planners identify
and act on external risk before it hits their delivery reliability?"*

```bash
python -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python run.py demo         # the whole pipeline, in the terminal
.venv/bin/python run.py inputs       # what is real / standing in / absent
.venv/bin/python -m streamlit run dashboard/app.py    # the dashboard
.venv/bin/python -m pytest -q        # 51 tests
```

Everything runs **offline**. No API key, no network, no model required.

---

## What this is

Nine pipeline stages behind a four-pane dashboard:

```
1. ingest     shipments + gauges + feeds
2. resolve    many reports -> one event
3. gate       does it touch our freight at all?   <- ~95% die here
4. variables  which of the 45 are active
5. reason     extraction + relevance, with evidence
6. simulate   delay distribution -> P(late)
7. score      CHF, lead time, matrix cell
8. act        options, contacts, decision deadline
9. export     store, CSV, per-event report
```

Stages 1–8 need no LLM. The Rhine demo runs end to end on numeric feeds, so if
the model is unavailable on the day the board still fills.

### The output is a sentence, not a score

> *"Switch barge leg to rail: costs CHF 2,361, avoids CHF 26,484 of expected
> loss. Net benefit CHF 24,123. Decide by Fri 25 Sep 13:43 UTC (7 days)."*

---

## The three failures the client named, and where each is answered

| Their words | Where |
|---|---|
| *"No filter for what is relevant on which freight lane"* | `engine/gate/` — spatial ∧ temporal ∧ modal, **zero parameters** |
| *"Disruptions are taken serious when carriers call — response options have already narrowed"* | `engine/portfolio/decay.py` — the option-decay curve |
| *"Even when a crisis is identified, it is unclear what to do and who to involve"* | `engine/act/playbook.py` — options, owners, named contacts, deadline |

---

## What Sika's answers changed

Six questions went to Sika. Four of the answers changed the build.

**Q1 — no risk ledger exists.** So `config.example/variables.yaml` *is* the
proposed ledger, written to be read and argued with by the planning team.
**45 fully specified variables, not 100 half-specified ones** — §5.0's claim
that every parameter is "something a planner already knows" only survives if
every entry is genuinely specified.

**Q3 — economics/logistics backgrounds; can read a matrix and play with the
assumptions; will not read the compute.** So every number has a plain-language
form, the formula sits behind a *"show the arithmetic"* toggle, and the
assumption table is **editable live in the sidebar**. That turns the weakest
point in the design — numbers we invented — into the most engaging moment in
the demo: hand them the laptop and let them set *"Antwerp strike = 4 days, not
3"*.

**Q5 — factor in social media.** Tier-3 sources are carried but cannot trip the
convene rule until corroborated. The point worth making out loud: social media
is not a *better* signal, it is an *earlier* one. Its whole value is arriving
while options are still cheap — a lead-time argument, so it belongs on the
decay curve rather than in the accuracy story.

**Q6 — the answer that reframed the product.**

> *"In crisis, established teams that meet on a weekly schedule (Procurement,
> Manufacturing, Supply Chain, Controlling) increase meeting frequency. They
> have full authority to decide on mitigation. **The real problem is that we
> declare a crisis too late and lose on available options.**"*

Authority is not the bottleneck. Analysis is not the bottleneck. **The decision
to convene is.** So the board gained a portfolio state strip and an option-decay
curve — see below.

---

## The option-decay curve

This is not a new model. The brief already computes
`decision_deadline = impact_time − action_duration`. That number exists only
because options **expire** — so the real decision, even for one shipment, is
never "act or don't" but **act now vs. wait**:

```
Value(act now) = S − C
Value(wait)    = P(still actionable at t+Δ) × E[S − C | info at t+Δ]
```

Once `t` crosses the deadline, `Value(wait) = 0`. Plot it and you get a step
function that drops each time an action class expires. The portfolio version is
the sum:

```
R(t) = Σ over at-risk shipments  max over still-feasible actions
       Value_of_acting(shipment, action, t)
```

**Zero new parameters.** The cliff edges are the `min_action_hours` table the
brief already mandates.

### The part that changes behaviour is not technical

Teams do not declare late because they lack a number. They declare late because
declaring is socially expensive — somebody has to stick their neck out and risk
crying wolf. So the tool does not argue for a crisis. It reports that a
threshold **the group pre-agreed in calm conditions** has tripped:

> *"Convene rule (agreed 12 Mar): recoverable value at risk > CHF 150k, or >4
> contracts exposed. Both tripped 06:14 today."*

That moves the decision from a judgement one person owns to a rule the group
already owns. It costs about forty lines of YAML and it is the whole mechanism.

**This does not violate BRIEF §12.** There is no global P×I matrix, and the
per-event matrix stays hidden until a dot is clicked. A one-line portfolio
posture is not a matrix.

---

## Four corrections to the brief's maths

Each would have quietly hollowed out a headline number. Each has a test.

**1 · The baseline must not take the best alternate.** §5.2 says compute delay
on each feasible route and take the minimum; §5.4 then wants
`E[loss|nothing] − E[loss|act]`. Both cannot hold — if the baseline already
routes around the disruption, the baseline has already acted and the value of
acting collapses to zero on exactly the shipments that have alternatives. The
minimum belongs inside the *act* scenario, priced.

**2 · Lateness is not delay.** `E[delay]` is measured against the planned ETA;
penalties accrue against the date promised to the customer. Those differ by the
commitment slack, and charging across the gap invents money. Also `max(0, ·)`
is convex, so it must be evaluated inside the Monte Carlo, never applied to the
mean.

**3 · Event draws must be shared across shipments.** Drawn per shipment, forty
shipments calling at Antwerp experience forty *independent* strikes. That is
physically impossible and it flattens the tail — which is the entire quantity
the convene decision depends on. Durations are drawn once per iteration into an
`(n_draws × n_events)` matrix and shared. Summing across shipments *within* a
draw then gives the correctly correlated portfolio distribution for free.

**4 · `probability_unknown` had nowhere to go.** §8.1 forbids absences becoming
values, but §5.7's matrix has an x-axis of P(late) from 0 to 1 — so an event
whose probability cannot be sourced would land at 0.5, which is the sibling
project's exact bug reappearing in the UI. Unsourced events are drawn in a
separate band **outside** the axis.

---

## The Rhine, and the detail they will catch

**Low water is a payload derate, not a stoppage.** Vessels keep sailing; they
load to reduced draught. The same tonnage then needs more sailings, and
low-water surcharges apply. Modelling it as "barge blocked" is wrong and a Sika
logistics colleague will know it is wrong within one sentence.

**The OTIF consequence runs through "in full", not only "on time".** A derate
that splits one consignment across two sailings fails OTIF even when the first
part arrives early. That is the mechanism no generic weather alert would find,
and it is why this lane is worth demonstrating.

> ⚠️ The Kaub band thresholds in `config.example/thresholds.yaml` are marked
> `source: assumed`. They reproduce the published *shape* but the exact
> centimetre values have **not** been sourced against official WSV/BfG loading
> tables. Source and cite them before any client demo.

---

## Deliberate deviations from the brief

| Brief says | Here | Why |
|---|---|---|
| ~100 risk variables | **45**, fully specified | Q1: no ledger exists, so this file *is* the proposal. 100 half-specified entries contradict §5.0's own claim |
| OSMnx for road/rail routing | Buffered great-circle corridors | Overpass at runtime + geopandas/GEOS chain, for 10–15 named lanes. Ships as a visibly empty socket |
| Shipment-level `etd`/`eta` | **Per-leg** planned windows | The temporal gate asks when the shipment transits *that node* — unanswerable without them |
| Plain JS + SVG + FastAPI | Streamlit + Plotly | Python-built charts are testable; the assumption editor is ~10 lines instead of a day |
| MapLibre vendored | Plotly `Scattergeo` + vendored topojson | folium/Leaflet needs a CDN. See below |
| No global view (§12) | One-line **state strip** | Q6. Not a matrix; the per-event matrix is untouched |

### The map

The starting prototype used folium. Rebuilding it surfaced a blocker that is
not a style problem: **folium loads Leaflet from a CDN**, so under a locked-down
network the map renders at *zero height* — not degraded, absent. It also broke
the brief's own §8.7 rule (*no build step, no CDN*).

Plotly's JS is served locally by Streamlit and `Scattergeo` ships land, ocean,
country, coastline and river geometry — the same Natural Earth layers the
prototype loaded five shapefiles to draw. Its topojson is
[vendored](dashboard/static/topojson/README.md) rather than fetched, so the map
renders with the network cable pulled out.

Other fixes from the prototype: `returned_objects=[]` was discarding every
click (which made click-a-dot-to-open-its-matrix structurally impossible);
shapefiles were re-read on every rerun; ~200 DivIcon labels were decoration
competing with a dozen nodes of information; `m.save()` wrote to disk on every
interaction; and hardcoded vessels and a hardcoded Suez disruption were
unconnected to any engine.

---

## Engineering rules that are load-bearing

**Nothing reads the wall clock.** Every stage takes an explicit `as_of`
(`engine/clock.py`). Three things fall out: the demo is reproducible, the
hindcast is nearly free (a past `as_of` plus archived feeds through the
identical code path), and the decay curve is expressible at all. A test walks
the AST of `engine/` to enforce it.

**An absence never becomes a value.** Unknown probability is `None` plus a
reason. An unconfigured action time is `None`, never a zero that would make the
action look permanently available. A node with no alternatives says so.

**Abstention is structurally valid.** `Extraction | Abstention` is a
discriminated union. "Every field required" plus "you must answer" is a
hallucination generator — the model invents values to satisfy the schema.

**Empty sockets are visibly empty.** The `/inputs` panel lists every input as
connected / example stand-in / absent, with one line on what each absence
costs. Nothing is silently defaulted.

**Provenance on every number**, extended to numeric config: a made-up wind
threshold is exactly as dangerous as a made-up quote, so every threshold
carries `source:` and assumed ones say so on screen.

**The funnel is measured, not asserted.** §3.2's 10,000 → 20 is a design
sketch; `/inputs` shows this run's real counts.

---

## Repository

```
engine/            institution-agnostic core — knows nothing about Sika
  clock.py         the as-of discipline
  schemas.py       every Pydantic model
  network/         nodes, corridors, geometry (no OSMnx)
  ingest/          shipments, gauges, feeds, socket contract
  variables/       the mask; rules.py is the CHALLENGER, not a fallback
  gate/            spatial ∧ temporal ∧ modal
  simulate/        portfolio draw matrix, buffer propagation
  score/           CHF, lead time, matrix bands
  act/             playbook, owners, contacts
  portfolio/       option decay, convene rule
config.example/    public, synthetic, committed
config/            gitignored — the customer's real data
dashboard/         Streamlit UI + vendored topojson
data/fixtures/     committed sample feeds, labelled
tests/             51 tests
```

`engine/` never imports from `dashboard/` or reads `config/` directly.
Onboarding a new customer is a profile swap — that is true here, not a claim.

---

## Known gaps

- **Everything is synthetic and labelled synthetic.** Shipments are generated;
  the news corpus is written by us; the Kaub series is a shaped reconstruction.
- **The reasoning layer is specified, not wired.** The rules router runs as the
  challenger; `reason/` is the next build step.
- **Kaub thresholds need sourcing** (above).
- **No accuracy claim is made.** The honest metric is the hindcast — did we fire
  before the carrier called? — and it has not run yet. Agreement between rules
  and model is worth shipping as a diagnostic but is *not* validation: both were
  written by the same people from the same variable list, so their errors
  correlate.

## Open questions for Sika

1. Do your contracts actually carry per-day delay penalties, or is the real cost
   expediting plus customer escalation? (The cost model has three components so
   it survives either answer, but this decides which one carries the weight.)
2. Would you pre-agree a convene threshold in calm conditions?
3. Does the shipment data include actual vs. planned arrival dates, or only
   lanes and volumes? (Decides whether the hindcast produces a *measured*
   lead-time claim or a plausible one.)
4. In a Rhine low-water week, what actually happens today — partial loading,
   rail switch, surcharge acceptance, deferral?
5. Who *doesn't* have authority, and above what CHF?
